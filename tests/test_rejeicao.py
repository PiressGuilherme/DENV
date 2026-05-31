"""Testes de rejeição de amostras (estado terminal alternativo).

Regras (decisão do usuário):
    - rejeitar SÓ amostras pendentes (não coletadas e não já rejeitadas);
    - motivo obrigatório, um de db.MOTIVOS_REJEICAO;
    - reverter devolve a amostra a Pendente;
    - rejeitada sai das demais fases (partição continua completa e disjunta);
    - rejeitada não reentra no fluxo;
    - migração leve adiciona as colunas a bancos pré-existentes.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from src import db
from src.db import TransicaoInvalida


@pytest.fixture
def con(tmp_path):
    c = db.init_db(tmp_path / "rej.db")
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
# rejeitar                                                                     #
# --------------------------------------------------------------------------- #
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
            "SELECT valor_novo FROM eventos WHERE chave='D1/25' AND campo='rejeitada'"
        ).fetchone()
        assert ev[0] == "Não Encontrada"

    def test_nao_rejeita_coletada(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        assert _row(con, "D1/25")["rejeitada"] == 0

    def test_lote_misto_recusa_tudo(self, con):
        db.avancar_fase(con, ["D1/25"], "coletada")  # D1 não-pendente
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25", "D2/25"], "Volume Insuficiente")
        # nada rejeitado (atômico)
        assert _row(con, "D2/25")["rejeitada"] == 0

    def test_rejeitar_idempotente_no_op(self, con):
        assert db.rejeitar(con, ["D1/25"], "Volume Insuficiente") == 1
        # segunda vez: já rejeitada, não é mais pendente -> recusa
        with pytest.raises(TransicaoInvalida):
            db.rejeitar(con, ["D1/25"], "Volume Insuficiente")


# --------------------------------------------------------------------------- #
# reverter                                                                     #
# --------------------------------------------------------------------------- #
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
            "SELECT COUNT(*) FROM eventos WHERE chave='D1/25' AND campo='rejeitada' "
            "AND valor_novo='0'"
        ).fetchone()[0]
        assert n == 1

    def test_revertida_pode_ser_coletada(self, con):
        """Após reverter, a amostra volta a ser elegível para o fluxo."""
        db.rejeitar(con, ["D1/25"], "Não Encontrada")
        db.reverter_rejeicao(con, ["D1/25"])
        assert db.avancar_fase(con, ["D1/25"], "coletada") == 1


# --------------------------------------------------------------------------- #
# Partição de fases com rejeitada                                             #
# --------------------------------------------------------------------------- #
class TestParticaoComRejeitada:
    def test_rejeitada_sai_de_pendente(self, con):
        db.rejeitar(con, ["D1/25"], "Volume Insuficiente")
        # D1 não aparece em 'pendente'
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
                f"SELECT COUNT(*) FROM amostras WHERE chave='D1/25' AND {clausula}"
            ).fetchone()[0]
        ]
        assert presencas == ["rejeitada"]


# --------------------------------------------------------------------------- #
# Migração de banco pré-existente                                             #
# --------------------------------------------------------------------------- #
class TestMigracao:
    def test_adiciona_colunas_em_banco_antigo(self, tmp_path):
        # Reproduz o schema v1 (pré-rejeição) removendo as 3 colunas novas do
        # _SCHEMA atual — garante paridade exata com o banco antigo real.
        db_path = tmp_path / "antigo.db"
        raw = sqlite3.connect(str(db_path))
        schema_v1 = db._SCHEMA
        for coluna in db._COLUNAS_MIGRACAO:
            schema_v1 = re.sub(rf"\n\s*{coluna}\s+[^,]+,", "", schema_v1)
        raw.executescript(schema_v1)
        raw.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade) "
            "VALUES ('D1/25', 'D', 1, 2025)"
        )
        raw.commit()
        raw.close()
        # Confirma que o v1 realmente não tem as colunas novas.
        chk = sqlite3.connect(str(db_path))
        cols0 = {r[1] for r in chk.execute("PRAGMA table_info(amostras)").fetchall()}
        chk.close()
        assert "rejeitada" not in cols0

        # init_db deve migrar (ALTER TABLE) sem perder dados.
        con = db.init_db(db_path)
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(amostras)").fetchall()}
            assert {"rejeitada", "motivo_rejeicao", "data_rejeicao"} <= cols
            # dado preservado e rejeitar funciona
            assert db.contar(con) == 1
            assert db.rejeitar(con, ["D1/25"], "Volume Insuficiente") == 1
        finally:
            con.close()

    def test_migracao_idempotente(self, tmp_path):
        """Rodar init_db duas vezes não falha nem duplica colunas."""
        db_path = tmp_path / "idem.db"
        db.init_db(db_path).close()
        con = db.init_db(db_path)  # segunda vez
        try:
            cols = [row[1] for row in con.execute("PRAGMA table_info(amostras)").fetchall()]
            assert cols.count("rejeitada") == 1
        finally:
            con.close()
