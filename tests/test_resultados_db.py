"""Testes do import de resultados contra o banco (gravação, guarda, migração)."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src import db, resultados


@pytest.fixture
def con(_pg_schema_con):
    c = _pg_schema_con
    for chave, num, ano in [
        ("D1/25", 1, 2025), ("D2/25", 2, 2025), ("D3/25", 3, 2025),
        ("D1264/26", 1264, 2026),
    ]:
        c.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade, "
            "ni_original) VALUES (%s, 'D', %s, %s, %s)",
            (chave, num, ano, chave),
        )
    c.commit()
    return c


def _ate_pcr(con, *chaves):
    for etapa in ("coletada", "extraida", "pcr_feito"):
        db.avancar_fase(con, list(chaves), etapa)


def _row(con, chave):
    return con.execute("SELECT * FROM amostras WHERE chave=%s", (chave,)).fetchone()


def _arquivo(dados: dict) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(dados).to_excel(buf, index=False)
    return buf.getvalue()


def _importar(con, dados: dict):
    """Ponta a ponta: bytes -> plano -> gravação. Devolve (plano, gravadas)."""
    df = resultados.ler_arquivo(_arquivo(dados), "r.xlsx")
    plano = resultados.montar_plano(df, db.indice_para_resultados(con))
    return plano, db.gravar_resultados(con, plano.registros())


class TestGravacao:
    def test_grava_ct(self, con):
        _ate_pcr(con, "D1/25")
        plano, n = _importar(con, {
            "NI": ["D1/25"], "DEN1": [None], "DEN2": [24.3],
            "DEN3": [None], "DEN4": [None],
        })
        assert n == 1
        r = _row(con, "D1/25")
        assert float(r["den2_ct"]) == 24.3
        assert r["den1_ct"] is None
        assert r["data_resultado"] is not None

    def test_coinfeccao(self, con):
        _ate_pcr(con, "D1/25")
        _importar(con, {"NI": ["D1/25"], "DEN1": [22.0], "DEN2": [30.5]})
        r = _row(con, "D1/25")
        assert float(r["den1_ct"]) == 22.0 and float(r["den2_ct"]) == 30.5
        assert resultados.sorotipo_de(r) == "DENV-1+2"

    def test_registra_evento_de_auditoria(self, con):
        _ate_pcr(con, "D1/25")
        _importar(con, {"NI": ["D1/25"], "DEN2": [24.3]})
        ev = con.execute(
            "SELECT valor_novo FROM eventos WHERE chave=%s AND campo=%s",
            ("D1/25", "resultado_den"),
        ).fetchone()
        assert ev is not None and "24.3" in ev["valor_novo"]

    def test_lote_grava_varias(self, con):
        _ate_pcr(con, "D1/25", "D2/25", "D3/25")
        _, n = _importar(con, {
            "NI": ["D1/25", "D2/25", "D3/25"],
            "DEN1": [20.0, None, None],
            "DEN2": [None, 25.0, None],
            "DEN3": [None, None, 30.0],
        })
        assert n == 3
        assert float(_row(con, "D3/25")["den3_ct"]) == 30.0

    def test_lote_vazio(self, con):
        assert db.gravar_resultados(con, []) == 0


class TestNaoSobrescreve:
    def test_reimportar_e_idempotente(self, con):
        _ate_pcr(con, "D1/25")
        dados = {"NI": ["D1/25"], "DEN2": [24.3]}
        _, n1 = _importar(con, dados)
        assert n1 == 1

        # 2ª rodada com valor DIFERENTE: nada muda e o motivo é reportado.
        plano2, n2 = _importar(con, {"NI": ["D1/25"], "DEN2": [99.9]})
        assert n2 == 0
        assert not plano2.aplicaveis
        assert plano2.ignoradas[0].motivo == resultados.MOTIVO_JA_TEM_RESULTADO
        assert float(_row(con, "D1/25")["den2_ct"]) == 24.3

    def test_guarda_sql_bloqueia_mesmo_sem_triagem(self, con):
        """gravar_resultados é seguro por si só, não só pela triagem prévia.

        Simula a corrida entre dois imports simultâneos: o plano foi montado
        quando a amostra ainda estava vazia, mas outro import gravou antes.
        """
        _ate_pcr(con, "D1/25")
        assert db.gravar_resultados(con, [{"chave": "D1/25", "den1_ct": 20.0}]) == 1
        # Plano "velho" tentando gravar por cima:
        assert db.gravar_resultados(con, [{"chave": "D1/25", "den1_ct": 99.9}]) == 0
        assert float(_row(con, "D1/25")["den1_ct"]) == 20.0


class TestTriagemContraBancoReal:
    def test_fora_de_fase_nao_grava(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")  # ainda não fez PCR
        plano, n = _importar(con, {"NI": ["D1/25"], "DEN1": [24.3]})
        assert n == 0
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_FORA_DE_FASE
        assert _row(con, "D1/25")["den1_ct"] is None

    def test_resolve_amostra_reclassificada_2026(self, con):
        """NI 'D1264/25' está gravado como 'D1264/26' (ano-de-verdade)."""
        _ate_pcr(con, "D1264/26")
        plano, n = _importar(con, {"NI": ["D1264/25"], "DEN3": [28.1]})
        assert n == 1
        assert plano.aplicaveis[0].chave == "D1264/26"
        assert float(_row(con, "D1264/26")["den3_ct"]) == 28.1

    def test_sequenciada_recebe_resultado_atrasado(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        _, n = _importar(con, {"NI": ["D1/25"], "DEN1": [24.3]})
        assert n == 1

    def test_arquivo_misto_reporta_cada_motivo(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D2/25"], "coletada")
        plano, n = _importar(con, {
            "NI": ["D1/25", "D2/25", "D999/25", "lixo"],
            "DEN1": [24.3, 24.3, 24.3, 24.3],
        })
        assert n == 1
        motivos = {l.motivo for l in plano.ignoradas}
        assert motivos == {
            resultados.MOTIVO_FORA_DE_FASE,
            resultados.MOTIVO_NAO_ENCONTRADO,
            resultados.MOTIVO_NI_INVALIDO,
        }


class TestFiltroSorotipo:
    def test_predicados_sql_exigem_ct_positivo(self):
        where, params = db.construir_filtro(sorotipo="DEN1")
        assert where == "den1_ct > 0"
        assert params == []

        where, params = db.construir_filtro(sorotipo=db.SOROTIPO_NAO_DETECTADO)
        assert "data_resultado IS NOT NULL" in where
        assert all(
            f"({campo} IS NULL OR {campo} <= 0)" in where
            for campo in db.COLUNAS_CT
        )
        assert params == []

    def test_filtra_por_sorotipo(self, con):
        _ate_pcr(con, "D1/25", "D2/25")
        _importar(con, {"NI": ["D1/25", "D2/25"],
                        "DEN1": [22.0, None], "DEN2": [None, 25.0]})
        where, params = db.construir_filtro(sorotipo="DEN2")
        chaves = {r["chave"] for r in db.listar_amostras(con, where=where, params=params)}
        assert chaves == {"D2/25"}

    def test_filtra_nao_detectado(self, con):
        _ate_pcr(con, "D1/25", "D2/25")
        db.gravar_resultados(con, [{"chave": "D1/25"}])   # resultado sem nenhum Ct
        _importar(con, {"NI": ["D2/25"], "DEN2": [25.0]})
        where, params = db.construir_filtro(sorotipo=db.SOROTIPO_NAO_DETECTADO)
        chaves = {r["chave"] for r in db.listar_amostras(con, where=where, params=params)}
        assert chaves == {"D1/25"}

    def test_sentinela_termociclador_nao_conta_como_detectado(self, con):
        _ate_pcr(con, "D1/25", "D2/25")
        db.gravar_resultados_termociclador(con, {
            "D1/25": {c: -1.0 for c in db.COLUNAS_CT},
            "D2/25": {"den1_ct": -1.0, "den2_ct": 24.3},
        })

        where, params = db.construir_filtro(sorotipo="DEN1")
        positivos_den1 = {
            r["chave"] for r in db.listar_amostras(con, where=where, params=params)
        }
        assert positivos_den1 == set()

        where, params = db.construir_filtro(sorotipo="DEN2")
        positivos_den2 = {
            r["chave"] for r in db.listar_amostras(con, where=where, params=params)
        }
        assert positivos_den2 == {"D2/25"}

        where, params = db.construir_filtro(sorotipo=db.SOROTIPO_NAO_DETECTADO)
        negativos = {
            r["chave"] for r in db.listar_amostras(con, where=where, params=params)
        }
        assert negativos == {"D1/25"}

    def test_filtra_com_e_sem_resultado(self, con):
        _ate_pcr(con, "D1/25", "D2/25")
        _importar(con, {"NI": ["D1/25"], "DEN1": [22.0]})
        where, params = db.construir_filtro(com_resultado=True)
        assert len(db.listar_amostras(con, where=where, params=params)) == 1
        where, params = db.construir_filtro(com_resultado=False)
        assert len(db.listar_amostras(con, where=where, params=params)) == 3

    def test_sorotipo_invalido_recusa(self):
        with pytest.raises(ValueError):
            db.construir_filtro(sorotipo="DEN9")


class TestMigracaoColunasNovas:
    def test_banco_legado_recebe_colunas(self, _pg_schema_con):
        """Banco sem as colunas novas ganha todas via _migrar (caso do Neon)."""
        con = _pg_schema_con
        novas = ["sequenciado", "data_sequenciado", "data_resultado", *db.COLUNAS_CT]
        for coluna in novas:
            con.execute(f"ALTER TABLE amostras DROP COLUMN IF EXISTS {coluna}")
        con.commit()
        assert not (set(novas) & db._colunas_existentes(con))

        adicionadas = db._migrar(con)
        assert set(adicionadas) == set(novas)
        assert set(novas) <= db._colunas_existentes(con)

    def test_migrar_e_no_op_quando_nada_falta(self, _pg_schema_con):
        """Em regime normal: zero ALTERs — é o que evita o lock do ed5160a."""
        assert db._migrar(_pg_schema_con) == []

    def test_criar_schema_duas_vezes(self, _pg_schema_con):
        db.criar_schema(_pg_schema_con)
        assert db.contar(_pg_schema_con) == 0

    def test_dados_de_reprocesso_sobrevivem_a_migracao(self, con):
        """A migração não pode zerar progresso já marcado."""
        _ate_pcr(con, "D1/25")
        con.execute("ALTER TABLE amostras DROP COLUMN IF EXISTS den1_ct")
        con.commit()
        db._migrar(con)
        r = _row(con, "D1/25")
        assert r["pcr_feito"] == 1 and r["data_pcr"] is not None
