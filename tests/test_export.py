"""Testes do export da visão atual."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src import db, export


@pytest.fixture
def con(_pg_schema_con):
    c = _pg_schema_con
    linhas = [
        ("D2/25",     2,     2025, "PORTO ALEGRE", "D2/25",     ""),
        ("D11633/25", 11633, 2025, "CANOAS",       "D11633/25", "ANO_NI_DIVERGE"),
        ("D5/25",     5,     2025, "GRAVATAI",     "D5/25",     ""),
    ]
    for chave, num, ano, mun, ni, flags in linhas:
        c.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade, "
            "municipio, ni_original, flags, data_coleta) "
            "VALUES (%s, 'D', %s, %s, %s, %s, %s, %s)",
            (chave, num, ano, mun, ni, flags, "2025-03-01"),
        )
    c.commit()
    db.avancar_fase(c, ["D2/25"], "coletada")
    db.rejeitar(c, ["D5/25"], "Volume Insuficiente")
    return c


def _df(con, where=None, params=()):
    rows = db.listar_amostras(con, where=where, params=params)
    return export.montar_dataframe(rows)


class TestColunas:
    def test_so_colunas_do_reprocesso(self, con):
        df = _df(con)
        cabec = list(df.columns)
        assert "NI" in cabec and "Fase" in cabec and "Motivo Rejeição" in cabec
        for proibida in ("Metodologia", "Status Exame", "1º Campo Resultado",
                         "Status", "Resultado"):
            assert proibida not in cabec

    def test_ordem_das_colunas(self, con):
        df = _df(con)
        assert df.columns[0] == "NI"
        assert df.columns[1] == "Número"


class TestConteudo:
    def test_ordenacao_canonica_preservada(self, con):
        df = _df(con)
        assert list(df["Número"]) == [2, 5, 11633]

    def test_booleanos_viram_sim_nao(self, con):
        df = _df(con).set_index("Número")
        assert df.loc[2, "Coletada"] == "Sim"
        assert df.loc[11633, "Coletada"] == "Não"

    def test_fase_derivada(self, con):
        df = _df(con).set_index("Número")
        assert df.loc[2, "Fase"] == "Coletada"
        assert df.loc[5, "Fase"] == "Rejeitada"
        assert df.loc[11633, "Fase"] == "Pendente"

    def test_motivo_rejeicao_presente(self, con):
        df = _df(con).set_index("Número")
        assert df.loc[5, "Motivo Rejeição"] == "Volume Insuficiente"

    def test_respeita_filtro(self, con):
        where, params = db.construir_filtro(municipio="CANOAS")
        df = _df(con, where, params)
        assert list(df["Número"]) == [11633]


class TestSerializacao:
    def test_xlsx_bytes_relegiveis(self, con):
        rows = db.listar_amostras(con)
        blob = export.para_xlsx_bytes(rows, sheet_name="geral")
        assert isinstance(blob, bytes) and len(blob) > 0
        df = pd.read_excel(io.BytesIO(blob))
        assert list(df["Número"]) == [2, 5, 11633]
        assert "NI" in df.columns

    def test_csv_bytes_relegiveis(self, con):
        rows = db.listar_amostras(con)
        blob = export.para_csv_bytes(rows)
        assert isinstance(blob, bytes) and len(blob) > 0
        assert blob[:3] == b"\xef\xbb\xbf"
        df = pd.read_csv(io.BytesIO(blob))
        assert list(df["Número"]) == [2, 5, 11633]

    def test_sheet_name_truncado(self, con):
        rows = db.listar_amostras(con)
        blob = export.para_xlsx_bytes(rows, sheet_name="x" * 40)
        assert isinstance(blob, bytes) and len(blob) > 0

    def test_export_vazio_gera_arquivo_so_cabecalho(self, con):
        where, params = db.construir_filtro(municipio="INEXISTENTE")
        rows = db.listar_amostras(con, where=where, params=params)
        blob = export.para_csv_bytes(rows)
        df = pd.read_csv(io.BytesIO(blob))
        assert len(df) == 0
        assert "NI" in df.columns
