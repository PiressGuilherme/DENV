"""Testes da edição manual de um resultado individual."""

from __future__ import annotations

import json

import pytest

from src import db, resultados


def _valores(**alteracoes) -> dict[str, object]:
    valores = {campo: None for campo in db.COLUNAS_RESULTADO}
    valores.update(alteracoes)
    return valores


class TestNormalizacao:
    @pytest.mark.parametrize("entrada, esperado", [
        (None, None),
        ("", None),
        ("  ", None),
        (24.3, 24.3),
        ("24,35", 24.35),
        ("14.105", 14.11),
        (50, 50.0),
    ])
    def test_valores_aceitos(self, entrada, esperado):
        assert db.normalizar_ct_edicao(entrada) == esperado

    @pytest.mark.parametrize(
        "entrada", [0, -1, 50.01, "abc", True, "NaN", "Infinity", object()]
    )
    def test_valores_recusados(self, entrada):
        with pytest.raises(db.EdicaoResultadoInvalida):
            db.normalizar_ct_edicao(entrada)

    def test_exige_os_seis_campos(self):
        with pytest.raises(db.EdicaoResultadoInvalida, match="faltando"):
            db.editar_resultado_manual(None, "D1/25", {"den1_ct": 24.3})


class _CursorFake:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _ConFake:
    def __init__(self, row):
        self.row = row
        self.comandos = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.comandos.append((sql, params))
        if "FOR UPDATE" in sql:
            return _CursorFake(self.row)
        return _CursorFake()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class TestOperacaoSemBancoExterno:
    def test_atualiza_somente_campos_realmente_alterados(self):
        row = {
            "pcr_feito": 1,
            "rejeitada": 0,
            "data_resultado": "2026-08-17",
            **{campo: None for campo in db.COLUNAS_RESULTADO},
            "den1_ct": -1.0,
            "den2_ct": 24.0,
        }
        con = _ConFake(row)

        resumo = db.editar_resultado_manual(
            con, "D1/25", _valores(den2_ct=25.0)
        )

        updates = [(sql, params) for sql, params in con.comandos if sql.startswith("UPDATE")]
        eventos = [params for sql, params in con.comandos if sql.startswith("INSERT INTO eventos")]
        assert resumo.campos_alterados == 1
        assert "den2_ct = %s" in updates[0][0]
        assert "den1_ct = %s" not in updates[0][0]
        assert updates[0][1] == [25.0, "D1/25"]
        payload = json.loads(eventos[0][2])
        assert payload["campos"] == [{
            "campo": "den2_ct",
            "valor_atual": 24.0,
            "valor_novo": 25.0,
        }]
        assert con.commits == 1
        assert con.rollbacks == 0

    def test_erro_de_fase_desfaz_transacao(self):
        row = {
            "pcr_feito": 0,
            "rejeitada": 0,
            "data_resultado": None,
            **{campo: None for campo in db.COLUNAS_RESULTADO},
        }
        con = _ConFake(row)

        with pytest.raises(db.EdicaoResultadoInvalida, match="PCR feito"):
            db.editar_resultado_manual(con, "D1/25", _valores(den1_ct=20.0))
        assert con.commits == 0
        assert con.rollbacks == 1


@pytest.fixture
def con(_pg_schema_con):
    con = _pg_schema_con
    for chave, numero in (("D1/25", 1), ("D2/25", 2)):
        con.execute(
            "INSERT INTO amostras "
            "(chave, prefixo, numero_sequencial, ano_verdade, ni_original) "
            "VALUES (%s, 'D', %s, 2025, %s)",
            (chave, numero, chave),
        )
    con.commit()
    for etapa in ("coletada", "extraida", "pcr_feito"):
        db.avancar_fase(con, ["D1/25"], etapa)
    db.avancar_fase(con, ["D2/25"], "coletada")
    return con


def _row(con, chave="D1/25"):
    return con.execute("SELECT * FROM amostras WHERE chave=%s", (chave,)).fetchone()


class TestPersistencia:
    def test_cria_resultado_e_recalcula_sorotipo(self, con):
        resumo = db.editar_resultado_manual(
            con,
            "D1/25",
            _valores(den2_ct=24.345, ci_1_4_ct=18.2, ci_2_3_ct=19.1),
        )

        row = _row(con)
        assert resumo.alterado is True
        assert resumo.campos_alterados == 3
        assert resumo.resultado_criado is True
        assert float(row["den2_ct"]) == 24.35
        assert float(row["ci_1_4_ct"]) == 18.2
        assert row["den1_ct"] is None
        assert row["data_resultado"] is not None
        assert resultados.sorotipo_de(row) == "DENV-2"

    def test_limpa_ct_existente_e_mantem_resultado_negativo(self, con):
        db.editar_resultado_manual(con, "D1/25", _valores(den1_ct=22.0))
        resumo = db.editar_resultado_manual(con, "D1/25", _valores())

        row = _row(con)
        assert resumo.alterado is True
        assert resumo.campos_alterados == 1
        assert resumo.resultado_criado is False
        assert row["den1_ct"] is None
        assert row["data_resultado"] is not None
        assert resultados.sorotipo_de(row) == "Não detectado"

    def test_auditoria_guarda_valores_anteriores_e_novos(self, con):
        db.editar_resultado_manual(con, "D1/25", _valores(den4_ct=28.4))
        db.editar_resultado_manual(con, "D1/25", _valores(den4_ct=30.1))

        evento = con.execute(
            "SELECT valor_novo FROM eventos "
            "WHERE chave=%s AND campo=%s ORDER BY id DESC LIMIT 1",
            ("D1/25", "resultado_manual"),
        ).fetchone()
        payload = json.loads(evento["valor_novo"])
        assert payload["resultado_criado"] is False
        assert payload["campos"] == [{
            "campo": "den4_ct",
            "valor_atual": 28.4,
            "valor_novo": 30.1,
        }]

    def test_salvar_sem_mudanca_nao_cria_evento(self, con):
        valores = _valores(den3_ct=25.0)
        db.editar_resultado_manual(con, "D1/25", valores)
        antes = con.execute(
            "SELECT COUNT(*) AS n FROM eventos WHERE campo=%s",
            ("resultado_manual",),
        ).fetchone()["n"]

        resumo = db.editar_resultado_manual(con, "D1/25", valores)
        depois = con.execute(
            "SELECT COUNT(*) AS n FROM eventos WHERE campo=%s",
            ("resultado_manual",),
        ).fetchone()["n"]
        assert resumo.alterado is False
        assert resumo.campos_alterados == 0
        assert depois == antes

    def test_recusa_amostra_fora_de_pcr_feito(self, con):
        with pytest.raises(db.EdicaoResultadoInvalida, match="PCR feito"):
            db.editar_resultado_manual(con, "D2/25", _valores(den1_ct=20.0))
        assert _row(con, "D2/25")["data_resultado"] is None

    def test_recusa_amostra_inexistente(self, con):
        with pytest.raises(db.EdicaoResultadoInvalida, match="não encontrada"):
            db.editar_resultado_manual(con, "D999/25", _valores(den1_ct=20.0))
