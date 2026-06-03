"""Testes do fluxo de fases (kanban) — avanço estrito, retrocesso, partição."""

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


# --------------------------------------------------------------------------- #
# avancar_fase — lote, campo, data, evento                                     #
# --------------------------------------------------------------------------- #
class TestAvancar:
    def test_marca_coletada_em_lote(self, con):
        n = db.avancar_fase(con, ["D1/25", "D2/25"], "coletada")
        assert n == 2
        for chave in ["D1/25", "D2/25"]:
            r = _row(con, chave)
            assert r["coletada"] == 1
            assert r["data_coletada"] is not None
        assert _row(con, "D3/25")["coletada"] == 0

    def test_avanca_nao_duplica(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        n = db.avancar_fase(con, ["D1/25"], "coletada")
        assert n == 0

    def test_avanca_fluxo_completo(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        db.avancar_fase(con, ["D1/25"], "pcr_feito")
        r = _row(con, "D1/25")
        assert r["coletada"] == r["extraida"] == r["pcr_feito"] == 1
        assert r["data_pcr"] is not None

    def test_avanca_sem_prereq_recusa(self, con):
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25"], "extraida")

    def test_avanca_lote_misto_recusa_tudo(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25", "D2/25"], "extraida")
        assert _row(con, "D2/25")["extraida"] == 0

    def test_avanca_grava_evento(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        ev = con.execute(
            "SELECT valor_novo FROM eventos WHERE chave=%s AND campo=%s",
            ("D1/25", "coletada"),
        ).fetchone()
        assert ev["valor_novo"] == "1"


# --------------------------------------------------------------------------- #
# retroceder_fase                                                               #
# --------------------------------------------------------------------------- #
class TestRetroceder:
    def test_retrocede_coletada(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        n = db.retroceder_fase(con, ["D1/25"], "coletada")
        assert n == 1
        r = _row(con, "D1/25")
        assert r["coletada"] == 0
        assert r["data_coletada"] is None

    def test_retrocede_limpa_posteriores(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        db.retroceder_fase(con, ["D1/25"], "coletada")
        r = _row(con, "D1/25")
        assert r["coletada"] == 0 and r["extraida"] == 0

    def test_retrocede_nao_marcado_nao_altera(self, con):
        n = db.retroceder_fase(con, ["D1/25"], "coletada")
        assert n == 0

    def test_retrocede_grava_evento(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.retroceder_fase(con, ["D1/25"], "coletada")
        ev = con.execute(
            "SELECT valor_novo FROM eventos WHERE chave=%s AND campo=%s "
            "ORDER BY em DESC LIMIT 1",
            ("D1/25", "coletada"),
        ).fetchone()
        assert ev["valor_novo"] == "0"


# --------------------------------------------------------------------------- #
# Partição completa de fases                                                   #
# --------------------------------------------------------------------------- #
class TestParticao:
    def test_pendente_soma_total(self, con):
        cont = db.contagens_por_fase(con)
        assert cont["pendente"] == cont["total"] == 3

    def test_cada_amostra_em_exatamente_uma_fase(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D2/25"], "coletada")
        db.avancar_fase(con, ["D2/25"], "extraida")
        cont = db.contagens_por_fase(con)
        soma = sum(cont[f] for f in db.FASES)
        assert soma == cont["total"] == 3

    def test_retrocesso_devolve_a_pendente(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.retroceder_fase(con, ["D1/25"], "coletada")
        assert db.contagens_por_fase(con)["pendente"] == 3

    def test_lote_vazio_retorna_zero(self, con):
        assert db.avancar_fase(con, [], "coletada") == 0
        assert db.retroceder_fase(con, [], "coletada") == 0

    def test_etapa_desconhecida_levanta_valor_error(self, con):
        with pytest.raises(ValueError):
            db.avancar_fase(con, ["D1/25"], "fase_inexistente")
