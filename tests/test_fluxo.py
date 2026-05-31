"""Testes do fluxo de fases (kanban) — avanço estrito, retrocesso, partição.

A lógica de fluxo mora em db.py (avancar_fase/retroceder_fase/FASES). A UI
(app.py) é uma casca em cima disso, testada manualmente. Aqui cobrimos a parte
determinística e crítica: a máquina de estados das fases.
"""

from __future__ import annotations

import pytest

from src import db
from src.db import TransicaoInvalida


@pytest.fixture
def con(tmp_path):
    """DB temporário com 3 amostras mínimas, todas pendentes."""
    c = db.init_db(tmp_path / "fluxo.db")
    for chave, num in [("D1/25", 1), ("D2/25", 2), ("D3/25", 3)]:
        c.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade) "
            "VALUES (?, 'D', ?, 2025)",
            (chave, num),
        )
    c.commit()
    yield c
    c.close()


def _row(con, chave):
    return con.execute("SELECT * FROM amostras WHERE chave=?", (chave,)).fetchone()


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
        # terceira intacta
        assert _row(con, "D3/25")["coletada"] == 0

    def test_grava_evento_por_chave(self, con):
        db.avancar_fase(con, ["D1/25", "D2/25"], "coletada")
        n_ev = con.execute(
            "SELECT COUNT(*) FROM eventos WHERE campo='coletada' AND valor_novo='1'"
        ).fetchone()[0]
        assert n_ev == 2

    def test_idempotente_nao_reconta_alteracoes(self, con):
        assert db.avancar_fase(con, ["D1/25"], "coletada") == 1
        # segunda vez não altera nada (já está coletada)
        assert db.avancar_fase(con, ["D1/25"], "coletada") == 0

    def test_data_nao_eh_sobrescrita_no_remarcar(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        data1 = _row(con, "D1/25")["data_coletada"]
        db.avancar_fase(con, ["D1/25"], "coletada")  # no-op
        assert _row(con, "D1/25")["data_coletada"] == data1

    def test_avanco_sequencial_completo(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        db.avancar_fase(con, ["D1/25"], "pcr_feito")
        r = _row(con, "D1/25")
        assert (r["coletada"], r["extraida"], r["pcr_feito"]) == (1, 1, 1)
        assert r["data_extraida"] is not None and r["data_pcr"] is not None


# --------------------------------------------------------------------------- #
# Avanço ESTRITO — bloqueio (sobrepõe Seção 4)                                 #
# --------------------------------------------------------------------------- #
class TestEstrito:
    def test_extrair_sem_coletar_recusado(self, con):
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25"], "extraida")

    def test_recusa_nao_altera_nada(self, con):
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25"], "extraida")
        assert _row(con, "D1/25")["extraida"] == 0

    def test_pcr_sem_extrair_recusado(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25"], "pcr_feito")

    def test_lote_misto_recusa_tudo(self, con):
        """Se UMA chave do lote não tem pré-requisito, recusa o lote inteiro."""
        db.avancar_fase(con, ["D1/25"], "coletada")  # só D1 coletada
        with pytest.raises(TransicaoInvalida):
            db.avancar_fase(con, ["D1/25", "D2/25"], "extraida")  # D2 não coletada
        # nada foi extraído (transação atômica)
        assert _row(con, "D1/25")["extraida"] == 0


# --------------------------------------------------------------------------- #
# retroceder_fase — limpa etapa + posteriores                                 #
# --------------------------------------------------------------------------- #
class TestRetroceder:
    def test_desmarca_coletada_limpa_posteriores(self, con):
        # avança até PCR
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        db.avancar_fase(con, ["D1/25"], "pcr_feito")
        # retrocede coletada -> tudo zera
        n = db.retroceder_fase(con, ["D1/25"], "coletada")
        assert n == 1
        r = _row(con, "D1/25")
        assert (r["coletada"], r["extraida"], r["pcr_feito"]) == (0, 0, 0)
        assert r["data_coletada"] is None
        assert r["data_extraida"] is None
        assert r["data_pcr"] is None

    def test_desmarca_extraida_mantem_coletada(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        db.retroceder_fase(con, ["D1/25"], "extraida")
        r = _row(con, "D1/25")
        assert r["coletada"] == 1  # coletada permanece
        assert r["extraida"] == 0
        assert r["data_extraida"] is None

    def test_retroceder_grava_evento(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.retroceder_fase(con, ["D1/25"], "coletada")
        n = con.execute(
            "SELECT COUNT(*) FROM eventos WHERE campo='coletada' AND valor_novo='0'"
        ).fetchone()[0]
        assert n == 1


# --------------------------------------------------------------------------- #
# Partição por fase — cada amostra em exatamente uma fase                      #
# --------------------------------------------------------------------------- #
class TestParticao:
    def test_soma_das_fases_igual_total(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D2/25"], "coletada")
        db.avancar_fase(con, ["D2/25"], "extraida")
        cont = db.contagens_por_fase(con)
        soma = cont["pendente"] + cont["coletada"] + cont["extraida"] + cont["pcr_feito"]
        assert soma == cont["total"] == 3

    def test_amostra_em_exatamente_uma_fase(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        db.avancar_fase(con, ["D1/25"], "extraida")
        # D1 deve aparecer só na fase 'extraida'
        presencas = [
            f for f, clausula in db.FASES.items()
            if con.execute(
                f"SELECT COUNT(*) FROM amostras WHERE chave='D1/25' AND {clausula}"
            ).fetchone()[0]
        ]
        assert presencas == ["extraida"]

    def test_where_por_fase_invalida(self, con):
        with pytest.raises(ValueError):
            db.where_por_fase("inexistente")


# --------------------------------------------------------------------------- #
# Idempotência do importer x fases já marcadas                                 #
# --------------------------------------------------------------------------- #
class TestIdempotenciaComFase:
    def test_reimport_preserva_fase(self, tmp_path):
        from src.importer import XLSX_PADRAO, importar

        if not XLSX_PADRAO.exists():
            pytest.skip("planilha de origem ausente")

        db_path = tmp_path / "reimp.db"
        importar(XLSX_PADRAO, db_path, verificar_sanidade=False)
        c = db.conectar(db_path)
        try:
            chave = c.execute("SELECT chave FROM amostras LIMIT 1").fetchone()[0]
            db.avancar_fase(c, [chave], "coletada")
            db.avancar_fase(c, [chave], "extraida")
        finally:
            c.close()

        importar(XLSX_PADRAO, db_path, verificar_sanidade=False)
        c = db.conectar(db_path)
        try:
            r = c.execute(
                "SELECT coletada, extraida, data_extraida FROM amostras WHERE chave=?",
                (chave,),
            ).fetchone()
            assert r["coletada"] == 1 and r["extraida"] == 1
            assert r["data_extraida"] is not None
        finally:
            c.close()
