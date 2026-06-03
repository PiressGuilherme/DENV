"""Testes de rejeição de amostras (estado terminal alternativo)."""

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


class TestRejeitar:
    def test_rejeita_pendente_em_lote(self, con):
        n = db.rejeitar(con, ["D1/25", "D2/25"], "Volume Insuficiente")
        assert n == 2
        for chave in ["D1/25", "D2/25"]:
            r = _row(con, chave)
            assert r["rejeitada"] == 1
            assert r["motivo_rejeicao"] == "Volume Insuficiente"
            assert r["data_rejeicao"] is not None
        assert _row(con, "D3/25")["rejeitada"] == 0

    def test_motivo_invalido(self, con):
        with pytest.raises(ValueError):
            db.rejeitar(con, ["D1/25"], "Motivo Qualquer")

    @pytest.mark.parametrize("motivo", db.MOTIVOS_REJEICAO)
    def test_motivos_validos(self, con, motivo):
        assert db.rejeitar(con, ["D1/25"], motivo) == 1
        assert _row(con, "D1/25")["motivo_rejeicao"] == motivo

    def test_grava_evento(self, con):
        db.rejeitar(con, ["D1/25"], "Não Encontrada")
        ev = con.execute(
            "SELECT valor_novo FROM eventos WHERE chave=%s AND campo=%s",
            ("D1/25", "rejeitada"),
        ).fetchone()
        assert ev["valor_novo"] == "Não Encontrada"

    def test_nao_rejeita_coletada(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        assert _row(con, "D1/25")["rejeitada"] == 0

    def test_lote_misto_recusa_tudo(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25", "D2/25"], "Volume Insuficiente")
        assert _row(con, "D2/25")["rejeitada"] == 0

    def test_rejeitar_idempotente_no_op(self, con):
        assert db.rejeitar(con, ["D1/25"], "Volume Insuficiente") == 1
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25"], "Volume Insuficiente")


class TestReverter:
    def test_reverte_para_pendente(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        n = db.reverter_rejeicao(con, ["D1/25"])
        assert n == 1
        r = _row(con, "D1/25")
        assert r["rejeitada"] == 0
        assert r["motivo_rejeicao"] is None
        assert r["data_rejeicao"] is None

    def test_reverter_grava_evento(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        db.reverter_rejeicao(con, ["D1/25"])
        n = con.execute(
            "SELECT COUNT(*) AS n FROM eventos "
            "WHERE chave=%s AND campo=%s AND valor_novo=%s",
            ("D1/25", "rejeitada", "0"),
        ).fetchone()["n"]
        assert n == 1

    def test_revertida_pode_ser_coletada(self, con):
        db.rejeitar(con, ["D1/25"], "Não Encontrada")
        db.reverter_rejeicao(con, ["D1/25"])
        assert db.avancar_fase(con, ["D1/25"], "coletada") == 1


class TestParticaoComRejeitada:
    def test_rejeitada_sai_de_pendente(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        pend = {r["chave"] for r in db.listar_amostras(con, where=db.where_por_fase("pendente"))}
        assert "D1/25" not in pend
        rej = {r["chave"] for r in db.listar_amostras(con, where=db.where_por_fase("rejeitada"))}
        assert rej == {"D1/25"}

    def test_soma_das_fases_igual_total(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        db.avancar_fase(con, ["D2/25"], "coletada")
        cont = db.contagens_por_fase(con)
        soma = (cont["pendente"] + cont["coletada"] + cont["extraida"]
                + cont["pcr_feito"] + cont["rejeitada"])
        assert soma == cont["total"] == 3

    def test_amostra_em_exatamente_uma_fase(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        presencas = [
            f for f, clausula in db.FASES.items()
            if con.execute(
                f"SELECT COUNT(*) AS n FROM amostras WHERE chave=%s AND {clausula}",
                ("D1/25",),
            ).fetchone()["n"]
        ]
        assert presencas == ["rejeitada"]


class TestMigracao:
    def test_adiciona_colunas_em_banco_antigo(self, _pg_schema_con):
        """Em PostgreSQL ADD COLUMN IF NOT EXISTS é idempotente — verifica que
        rodar _migrar() numa tabela que já tem as colunas não levanta erro."""
        con = _pg_schema_con
        # Todas as colunas devem existir após criar_schema
        cols = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'amostras'"
            ).fetchall()
        }
        assert {"rejeitada", "motivo_rejeicao", "data_rejeicao"} <= cols
        # Rodar _migrar de novo não deve falhar (idempotente)
        db._migrar(con)
        assert db.contar(con) == 0

    def test_migracao_idempotente(self, _pg_schema_con):
        """Chamar criar_schema duas vezes no mesmo schema não falha."""
        con = _pg_schema_con
        db.criar_schema(con)  # segunda chamada
        assert db.contar(con) == 0
