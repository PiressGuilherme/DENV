"""Testes do importador — Fase 2 (Seção 7/8 do CLAUDE.md).

Cobre:
    - contagens de sanidade contra a planilha real (5.506 / 3.488 / 2.018 / 1.276);
    - schema criado corretamente;
    - IDEMPOTÊNCIA: reimportar não zera o progresso de reprocesso marcado, mas
      atualiza os campos descritivos.

Usa um DB temporário (tmp_path) — nunca toca o reprocesso.db real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import db
from src.importer import XLSX_PADRAO, importar

XLSX_EXISTE = XLSX_PADRAO.exists()
pytestmark = pytest.mark.skipif(
    not XLSX_EXISTE, reason="planilha de origem ausente em data/"
)


@pytest.fixture(scope="module")
def resultado_import(tmp_path_factory):
    """Importa uma vez para um DB temporário e reaproveita o resultado."""
    db_path = tmp_path_factory.mktemp("db") / "teste.db"
    r = importar(XLSX_PADRAO, db_path, verificar_sanidade=False)
    return r, db_path


# --------------------------------------------------------------------------- #
# Contagens de sanidade (Seção 7 passo 8)                                      #
# --------------------------------------------------------------------------- #
class TestContagens:
    def test_total_ignoradas(self, resultado_import):
        r, _ = resultado_import
        assert r.ignoradas_sem_ni == 1271
        assert r.ignoradas_ni_invalido == 5
        assert r.total_ignoradas == 1276

    def test_amostras_unicas(self, resultado_import):
        r, _ = resultado_import
        assert r.amostras_unicas == 5506

    def test_por_ano(self, resultado_import):
        r, _ = resultado_import
        assert r.por_ano.get(2025) == 3488
        assert r.por_ano.get(2026) == 2018

    def test_sem_anos_inesperados(self, resultado_import):
        r, _ = resultado_import
        assert set(r.por_ano) == {2025, 2026}

    def test_linhas_no_banco_batem(self, resultado_import):
        r, db_path = resultado_import
        con = db.conectar(db_path)
        try:
            assert db.contar(con) == r.amostras_unicas
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Schema + ordenação canônica                                                 #
# --------------------------------------------------------------------------- #
class TestSchemaEOrdenacao:
    def test_tabelas_existem(self, resultado_import):
        _, db_path = resultado_import
        con = db.conectar(db_path)
        try:
            nomes = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "amostras" in nomes
            assert "eventos" in nomes
        finally:
            con.close()

    def test_ordenacao_canonica_numero_como_int(self, resultado_import):
        """D1264 deve vir antes de D11633 no mesmo ano/prefixo."""
        _, db_path = resultado_import
        con = db.conectar(db_path)
        try:
            rows = db.listar_amostras(
                con, where="prefixo = 'D' AND ano_verdade = 2025"
            )
            nums = [r["numero_sequencial"] for r in rows]
            assert nums == sorted(nums)  # estritamente crescente como int
            # garante que não está ordenado como texto
            assert nums[0] < nums[-1]
        finally:
            con.close()

    def test_n_origem_minimo_um(self, resultado_import):
        _, db_path = resultado_import
        con = db.conectar(db_path)
        try:
            menor = con.execute("SELECT MIN(n_origem) FROM amostras").fetchone()[0]
            assert menor >= 1
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Idempotência (Seção 7 passo 7 — a exigência crítica)                          #
# --------------------------------------------------------------------------- #
class TestIdempotencia:
    def test_reimport_preserva_reprocesso(self, tmp_path):
        db_path = tmp_path / "idem.db"
        importar(XLSX_PADRAO, db_path, verificar_sanidade=False)

        con = db.conectar(db_path)
        try:
            chave = con.execute(
                "SELECT chave FROM amostras ORDER BY ano_verdade, prefixo, numero_sequencial LIMIT 1"
            ).fetchone()[0]
            # marca progresso de reprocesso
            con.execute(
                "UPDATE amostras SET coletada=1, extraida=1, "
                "data_coletada=CURRENT_TIMESTAMP WHERE chave=?",
                (chave,),
            )
            db.registrar_evento(con, chave, "coletada", "1")
            con.commit()
        finally:
            con.close()

        # reimporta
        r2 = importar(XLSX_PADRAO, db_path, verificar_sanidade=False)

        con = db.conectar(db_path)
        try:
            row = con.execute(
                "SELECT coletada, extraida, pcr_feito, data_coletada FROM amostras WHERE chave=?",
                (chave,),
            ).fetchone()
            assert row["coletada"] == 1, "progresso 'coletada' foi zerado no reimport!"
            assert row["extraida"] == 1, "progresso 'extraida' foi zerado no reimport!"
            assert row["pcr_feito"] == 0
            assert row["data_coletada"] is not None
            # reimport atualiza, não insere duplicata
            assert r2.amostras_unicas == 5506
            assert r2.inseridas == 0
            assert r2.atualizadas == 5506
        finally:
            con.close()

    def test_reimport_atualiza_descritivos(self, tmp_path):
        db_path = tmp_path / "desc.db"
        importar(XLSX_PADRAO, db_path, verificar_sanidade=False)

        con = db.conectar(db_path)
        try:
            chave = con.execute("SELECT chave FROM amostras LIMIT 1").fetchone()[0]
            # corrompe um descritivo
            con.execute("UPDATE amostras SET municipio='XXX' WHERE chave=?", (chave,))
            con.commit()
        finally:
            con.close()

        importar(XLSX_PADRAO, db_path, verificar_sanidade=False)

        con = db.conectar(db_path)
        try:
            muni = con.execute(
                "SELECT municipio FROM amostras WHERE chave=?", (chave,)
            ).fetchone()[0]
            assert muni != "XXX", "descritivo não foi restaurado pelo reimport"
        finally:
            con.close()
