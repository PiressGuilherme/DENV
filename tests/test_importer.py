"""Testes do importador — sanidade, schema, idempotência."""

from __future__ import annotations

import os

import pytest

from src import db
from src.importer import XLSX_PADRAO, importar

XLSX_EXISTE = XLSX_PADRAO.exists()
pytestmark = pytest.mark.skipif(
    not XLSX_EXISTE, reason="planilha de origem ausente em data/"
)


@pytest.fixture(scope="module")
def resultado_import(_pg_schema_con_module):
    """Importa uma vez para schema isolado e reaproveita no módulo."""
    r = importar(XLSX_PADRAO, _con=_pg_schema_con_module, verificar_sanidade=False)
    return r, _pg_schema_con_module


# --------------------------------------------------------------------------- #
# Contagens de sanidade                                                         #
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
        assert r.por_ano.get(2025) == 3415
        assert r.por_ano.get(2026) == 2091

    def test_sem_anos_inesperados(self, resultado_import):
        r, _ = resultado_import
        assert set(r.por_ano) == {2025, 2026}

    def test_linhas_no_banco_batem(self, resultado_import):
        r, con = resultado_import
        assert db.contar(con) == r.amostras_unicas


# --------------------------------------------------------------------------- #
# Schema + ordenação canônica                                                  #
# --------------------------------------------------------------------------- #
class TestSchemaEOrdenacao:
    def test_tabelas_existem(self, resultado_import):
        _, con = resultado_import
        nomes = {
            r["table_name"]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
        assert "amostras" in nomes
        assert "eventos" in nomes

    def test_ordenacao_canonica_numero_como_int(self, resultado_import):
        _, con = resultado_import
        rows = db.listar_amostras(
            con, where="prefixo = 'D' AND ano_verdade = 2025"
        )
        nums = [r["numero_sequencial"] for r in rows]
        assert nums == sorted(nums)
        assert nums[0] < nums[-1]

    def test_n_origem_minimo_um(self, resultado_import):
        _, con = resultado_import
        menor = con.execute(
            "SELECT MIN(n_origem) AS m FROM amostras"
        ).fetchone()["m"]
        assert menor >= 1


# --------------------------------------------------------------------------- #
# Idempotência                                                                  #
# --------------------------------------------------------------------------- #
class TestIdempotencia:
    def test_reimport_preserva_reprocesso(self, _pg_schema_con):
        con = _pg_schema_con
        importar(XLSX_PADRAO, _con=con, verificar_sanidade=False)

        chave = con.execute(
            "SELECT chave FROM amostras ORDER BY ano_verdade, prefixo, "
            "numero_sequencial LIMIT 1"
        ).fetchone()["chave"]

        con.execute(
            "UPDATE amostras SET coletada=1, extraida=1, "
            "data_coletada=CURRENT_TIMESTAMP WHERE chave=%s",
            (chave,),
        )
        db.registrar_evento(con, chave, "coletada", "1")
        con.commit()

        r2 = importar(XLSX_PADRAO, _con=con, verificar_sanidade=False)

        row = con.execute(
            "SELECT coletada, extraida, pcr_feito, data_coletada "
            "FROM amostras WHERE chave=%s",
            (chave,),
        ).fetchone()
        assert row["coletada"] == 1, "progresso 'coletada' foi zerado no reimport!"
        assert row["extraida"] == 1, "progresso 'extraida' foi zerado no reimport!"
        assert row["pcr_feito"] == 0
        assert row["data_coletada"] is not None
        assert r2.amostras_unicas == 5506
        assert r2.inseridas == 0
        assert r2.atualizadas == 5506

    def test_reimport_atualiza_descritivos(self, _pg_schema_con):
        con = _pg_schema_con
        importar(XLSX_PADRAO, _con=con, verificar_sanidade=False)

        chave = con.execute(
            "SELECT chave FROM amostras LIMIT 1"
        ).fetchone()["chave"]

        con.execute("UPDATE amostras SET municipio=%s WHERE chave=%s",
                    ("XXX", chave))
        con.commit()

        importar(XLSX_PADRAO, _con=con, verificar_sanidade=False)

        muni = con.execute(
            "SELECT municipio FROM amostras WHERE chave=%s", (chave,)
        ).fetchone()["municipio"]
        assert muni != "XXX", "descritivo não foi restaurado pelo reimport"
