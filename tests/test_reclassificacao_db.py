"""Testes da migração de reclassificação 2026 no banco (_reclassificar_2026).

Garante que bancos já populados (chave D{n}/25 + flag) são reconciliados para a
nova regra preservando progresso de reprocesso/rejeição e referências de eventos.
"""

from __future__ import annotations

import sqlite3

import pytest

from src import db


def _inserir(con, chave, prefixo, num, ano_verdade, ni_ano, flags="",
             data_coleta="2025-12-30"):
    con.execute(
        "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade, "
        "ni_ano, flags, data_coleta) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chave, prefixo, num, ano_verdade, ni_ano, flags, data_coleta),
    )


@pytest.fixture
def db_path_antigo(tmp_path):
    """Banco populado como ANTES da regra (D{n}/25 + ANO_NI_DIVERGE)."""
    path = tmp_path / "antigo.db"
    # cria schema via init_db, depois insere linhas com o estado pré-regra
    con = db.init_db(path)
    # apaga eventuais e insere casos controlados
    con.execute("DELETE FROM amostras")
    # candidata dentro do range, com progresso marcado
    _inserir(con, "D5/25", "D", 5, 2025, 2026, "ANO_NI_DIVERGE")
    # candidata no limite superior
    _inserir(con, "D976/25", "D", 976, 2025, 2026, "ANO_NI_DIVERGE")
    # fora do range -> NÃO deve mudar
    _inserir(con, "D977/25", "D", 977, 2025, 2026, "ANO_NI_DIVERGE")
    # prefixo diferente -> NÃO muda
    _inserir(con, "SR5/25", "SR", 5, 2025, 2026, "ANO_NI_DIVERGE")
    con.commit()
    # marca progresso na D5 e grava evento (precisa rodar antes da migração)
    con.execute("UPDATE amostras SET coletada=1, data_coletada=CURRENT_TIMESTAMP "
                "WHERE chave='D5/25'")
    db.registrar_evento(con, "D5/25", "coletada", "1")
    con.commit()
    con.close()
    return path


class TestMigracaoReclassificacao:
    def test_remapeia_chave_e_ano(self, db_path_antigo):
        con = db.init_db(db_path_antigo)  # dispara a migração
        try:
            # D5/25 -> D5/26, ano 2026, sem flag
            r = con.execute(
                "SELECT * FROM amostras WHERE chave='D5/26'"
            ).fetchone()
            assert r is not None
            assert r["ano_verdade"] == 2026
            assert "ANO_NI_DIVERGE" not in (r["flags"] or "")
            # a chave antiga não existe mais
            assert con.execute(
                "SELECT 1 FROM amostras WHERE chave='D5/25'"
            ).fetchone() is None
        finally:
            con.close()

    def test_preserva_progresso(self, db_path_antigo):
        con = db.init_db(db_path_antigo)
        try:
            r = con.execute(
                "SELECT coletada, data_coletada FROM amostras WHERE chave='D5/26'"
            ).fetchone()
            assert r["coletada"] == 1
            assert r["data_coletada"] is not None
        finally:
            con.close()

    def test_atualiza_eventos(self, db_path_antigo):
        con = db.init_db(db_path_antigo)
        try:
            # o evento gravado para D5/25 agora aponta para D5/26
            antigos = con.execute(
                "SELECT COUNT(*) FROM eventos WHERE chave='D5/25'"
            ).fetchone()[0]
            novos = con.execute(
                "SELECT COUNT(*) FROM eventos WHERE chave='D5/26'"
            ).fetchone()[0]
            assert antigos == 0
            assert novos == 1
            # sem violação de FK
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            con.close()

    def test_limite_superior_incluido(self, db_path_antigo):
        con = db.init_db(db_path_antigo)
        try:
            assert con.execute(
                "SELECT ano_verdade FROM amostras WHERE chave='D976/26'"
            ).fetchone()["ano_verdade"] == 2026
        finally:
            con.close()

    def test_fora_do_range_nao_muda(self, db_path_antigo):
        con = db.init_db(db_path_antigo)
        try:
            # D977 continua 2025 + flag
            r = con.execute(
                "SELECT ano_verdade, flags FROM amostras WHERE chave='D977/25'"
            ).fetchone()
            assert r["ano_verdade"] == 2025
            assert "ANO_NI_DIVERGE" in r["flags"]
            # SR também intacta
            assert con.execute(
                "SELECT 1 FROM amostras WHERE chave='SR5/25'"
            ).fetchone() is not None
        finally:
            con.close()

    def test_idempotente(self, db_path_antigo):
        db.init_db(db_path_antigo).close()
        con = db.init_db(db_path_antigo)  # 2ª vez não deve mudar mais nada
        try:
            anos = {r[0]: r[1] for r in con.execute(
                "SELECT ano_verdade, COUNT(*) FROM amostras GROUP BY ano_verdade"
            ).fetchall()}
            # D5, D976 em 2026; D977, SR5 em 2025
            assert anos.get(2026) == 2
            assert anos.get(2025) == 2
        finally:
            con.close()
