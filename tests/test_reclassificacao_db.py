"""Testes da migração de reclassificação 2026 no banco (_reclassificar_2026)."""

from __future__ import annotations

import pytest

from src import db


def _inserir(con, chave, prefixo, num, ano_verdade, ni_ano, flags="",
             data_coleta="2025-12-30"):
    con.execute(
        "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade, "
        "ni_ano, flags, data_coleta) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (chave, prefixo, num, ano_verdade, ni_ano, flags, data_coleta),
    )


@pytest.fixture
def con_antigo(_pg_schema_con):
    """Schema com dados PRÉ-regra; _reclassificar_2026 não rodou sobre eles."""
    con = _pg_schema_con
    # Schema está vazio (criar_schema rodou em _pg_schema_con mas banco vazio,
    # então _reclassificar_2026 foi no-op). Inserimos estado pré-regra agora:
    _inserir(con, "D5/25",   "D",  5,   2025, 2026, "ANO_NI_DIVERGE")
    _inserir(con, "D976/25", "D",  976, 2025, 2026, "ANO_NI_DIVERGE")
    _inserir(con, "D977/25", "D",  977, 2025, 2026, "ANO_NI_DIVERGE")  # fora do range
    _inserir(con, "SR5/25",  "SR", 5,   2025, 2026, "ANO_NI_DIVERGE")  # prefixo ≠ D
    con.commit()
    # Marca progresso em D5 antes da migração
    con.execute("UPDATE amostras SET coletada=1, data_coletada=CURRENT_TIMESTAMP "
                "WHERE chave=%s", ("D5/25",))
    db.registrar_evento(con, "D5/25", "coletada", "1")
    con.commit()
    return con


class TestMigracaoReclassificacao:
    def test_remapeia_chave_e_ano(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        r = con_antigo.execute(
            "SELECT * FROM amostras WHERE chave=%s", ("D5/26",)
        ).fetchone()
        assert r is not None
        assert r["ano_verdade"] == 2026
        assert "ANO_NI_DIVERGE" not in (r["flags"] or "")
        assert con_antigo.execute(
            "SELECT 1 FROM amostras WHERE chave=%s", ("D5/25",)
        ).fetchone() is None

    def test_preserva_progresso(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        r = con_antigo.execute(
            "SELECT coletada, data_coletada FROM amostras WHERE chave=%s", ("D5/26",)
        ).fetchone()
        assert r["coletada"] == 1
        assert r["data_coletada"] is not None

    def test_atualiza_eventos(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        # ON UPDATE CASCADE: eventos migram automaticamente para D5/26
        antigos = con_antigo.execute(
            "SELECT COUNT(*) AS n FROM eventos WHERE chave=%s", ("D5/25",)
        ).fetchone()["n"]
        novos = con_antigo.execute(
            "SELECT COUNT(*) AS n FROM eventos WHERE chave=%s", ("D5/26",)
        ).fetchone()["n"]
        assert antigos == 0
        assert novos == 1

    def test_limite_superior_incluido(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        assert con_antigo.execute(
            "SELECT ano_verdade FROM amostras WHERE chave=%s", ("D976/26",)
        ).fetchone()["ano_verdade"] == 2026

    def test_fora_do_range_nao_muda(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        r = con_antigo.execute(
            "SELECT ano_verdade, flags FROM amostras WHERE chave=%s", ("D977/25",)
        ).fetchone()
        assert r["ano_verdade"] == 2025
        assert "ANO_NI_DIVERGE" in r["flags"]
        assert con_antigo.execute(
            "SELECT 1 FROM amostras WHERE chave=%s", ("SR5/25",)
        ).fetchone() is not None

    def test_idempotente(self, con_antigo):
        db._reclassificar_2026(con_antigo)
        db._reclassificar_2026(con_antigo)  # segunda chamada não deve mudar nada
        anos = {
            r["ano_verdade"]: r["n"]
            for r in con_antigo.execute(
                "SELECT ano_verdade, COUNT(*) AS n FROM amostras GROUP BY ano_verdade"
            ).fetchall()
        }
        assert anos.get(2026) == 2   # D5/26, D976/26
        assert anos.get(2025) == 2   # D977/25, SR5/25
