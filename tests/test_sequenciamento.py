"""Testes da etapa 'Sequenciado' — sobreposição com PCR feito e invariantes."""

from __future__ import annotations

import pytest

from src import db
from src.db import TransicaoInvalida


@pytest.fixture
def con(_pg_schema_con):
    c = _pg_schema_con
    for chave, num in [("D1/25", 1), ("D2/25", 2), ("D3/25", 3)]:
        c.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade) "
            "VALUES (%s, 'D', %s, 2025)",
            (chave, num),
        )
    c.commit()
    return c


def _row(con, chave):
    return con.execute("SELECT * FROM amostras WHERE chave=%s", (chave,)).fetchone()


def _ate_pcr(con, *chaves):
    for etapa in ("coletada", "extraida", "pcr_feito"):
        db.avancar_fase(con, list(chaves), etapa)


def _chaves_da_fase(con, fase):
    return {
        r["chave"] for r in db.listar_amostras(con, where=db.where_por_fase(fase))
    }


class TestAvanco:
    def test_avanca_de_pcr_para_sequenciado(self, con):
        _ate_pcr(con, "D1/25")
        assert db.avancar_fase(con, ["D1/25"], "sequenciado") == 1
        r = _row(con, "D1/25")
        assert r["sequenciado"] == 1
        assert r["data_sequenciado"] is not None

    def test_exige_pcr_feito(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25"], "sequenciado")

    def test_registra_evento(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        ev = con.execute(
            "SELECT valor_novo FROM eventos WHERE chave=%s AND campo=%s",
            ("D1/25", "sequenciado"),
        ).fetchone()
        assert ev["valor_novo"] == "1"


class TestSobreposicao:
    """A decisão central: sequenciada CONTINUA em PCR feito."""

    def test_permanece_em_pcr_feito(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        assert "D1/25" in _chaves_da_fase(con, "pcr_feito")
        assert "D1/25" in _chaves_da_fase(con, "sequenciado")

    def test_etapa_exclusiva_remove_da_anterior(self, con):
        """Contraste: extraída SAI de coletada (exclusiva=True)."""
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        assert "D1/25" not in _chaves_da_fase(con, "coletada")

    def test_badge_mostra_a_etapa_mais_avancada(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        assert db.fase_da_linha(_row(con, "D1/25")) == "sequenciado"

    def test_contagens_sobrepoem_de_proposito(self, con):
        _ate_pcr(con, "D1/25", "D2/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        cont = db.contagens_por_fase(con)
        assert cont["pcr_feito"] == 2   # inclui a sequenciada
        assert cont["sequenciado"] == 1


class TestParticaoExclusiva:
    """FASES_EXCLUSIVAS continua sendo uma partição completa e disjunta."""

    def test_soma_igual_total_mesmo_com_sequenciada(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        db.rejeitar(con, ["D2/25"], "Volume Insuficiente")
        cont = db.contagens_por_fase(con)
        soma = sum(cont[f] for f in db.FASES_EXCLUSIVAS)
        assert soma == cont["total"] == 3

    def test_cada_amostra_em_exatamente_uma_fase_exclusiva(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        presencas = [
            f for f, clausula in db.FASES_EXCLUSIVAS.items()
            if con.execute(
                f"SELECT COUNT(*) AS n FROM amostras WHERE chave=%s AND {clausula}",
                ("D1/25",),
            ).fetchone()["n"]
        ]
        assert presencas == ["pcr_feito"]

    def test_sql_e_python_concordam(self, con):
        """db.FASES (SQL) e db.fase_da_linha (Python) não podem divergir.

        As duas derivações vivem lado a lado; este teste amarra uma na outra
        para cada combinação de estado que o fluxo consegue produzir.
        """
        db.rejeitar(con, ["D3/25"], "Não Encontrada")
        _ate_pcr(con, "D1/25", "D2/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        for r in db.listar_amostras(con):
            fase = db.fase_da_linha(r)
            n = con.execute(
                f"SELECT COUNT(*) AS n FROM amostras "
                f"WHERE chave=%s AND {db.FASES[fase]}",
                (r["chave"],),
            ).fetchone()["n"]
            assert n == 1, f"{r['chave']}: Python diz {fase}, SQL discorda"


class TestRetrocesso:
    def test_desmarcar_pcr_limpa_sequenciado(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        db.retroceder_fase(con, ["D1/25"], "pcr_feito")
        r = _row(con, "D1/25")
        assert r["pcr_feito"] == 0 and r["sequenciado"] == 0
        assert r["data_pcr"] is None and r["data_sequenciado"] is None

    def test_desmarcar_sequenciado_preserva_pcr(self, con):
        _ate_pcr(con, "D1/25")
        db.avancar_fase(con, ["D1/25"], "sequenciado")
        db.retroceder_fase(con, ["D1/25"], "sequenciado")
        r = _row(con, "D1/25")
        assert r["sequenciado"] == 0 and r["pcr_feito"] == 1


class TestRegistroDeEtapas:
    """O registro é a fonte da verdade: as derivações têm que bater com ele."""

    def test_fases_derivadas_cobrem_todas_as_etapas(self):
        for etapa in db.ETAPAS_DEF:
            assert etapa.chave in db.FASES
            assert etapa.chave in db.LABEL_FASE
            assert etapa.chave in db.COR_FASE

    def test_prerequisitos_encadeiam(self):
        anteriores: list[str] = []
        for etapa in db.ETAPAS_DEF:
            assert etapa.prerequisito in (None, *anteriores)
            anteriores.append(etapa.chave)

    def test_colunas_do_registro_existem_no_schema(self, _pg_schema_con):
        cols = db._colunas_existentes(_pg_schema_con)
        for etapa in db.ETAPAS_DEF:
            assert etapa.coluna in cols and etapa.coluna_data in cols
        assert set(db.COLUNAS_CT) <= cols and "data_resultado" in cols
