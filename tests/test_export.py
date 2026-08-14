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

    def test_cabecalhos_estaveis(self, con):
        """Trava de regressão: os cabeçalhos são derivados de db.ETAPAS_DEF, e
        uma derivação ingênua renomearia 'Data PCR' para 'Data PCR feito',
        quebrando planilhas já em uso na bancada."""
        cabec = list(_df(con).columns)
        for esperado in ("Coletada", "Data Coletada", "Extraída", "Data Extraída",
                         "PCR feito", "Data PCR", "Rejeitada", "Flags"):
            assert esperado in cabec, f"cabeçalho '{esperado}' sumiu do export"

    def test_colunas_de_resultado(self, con):
        cabec = list(_df(con).columns)
        assert "Sequenciada" in cabec and "Data Sequenciada" in cabec
        assert "Sorotipo" in cabec and "Data Resultado" in cabec
        for s in db.SOROTIPOS:
            assert f"{s} (Ct)" in cabec

    def test_ct_ausente_nao_vira_texto_none(self, con):
        """Ct nulo tem que sair como célula vazia, nunca como a string 'None'."""
        df = _df(con)
        assert not (df["DEN1 (Ct)"].astype(str) == "None").any()


class TestConteudo:
    def test_sentinela_ct_e_exportado_como_celula_vazia(self):
        assert export._valor({"den1_ct": -1.0}, "den1_ct") is None
        assert export._valor({"ci_1_4_ct": -1.0}, "ci_1_4_ct") is None

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

    def test_sentinela_nao_vaza_e_nao_gera_falso_sorotipo(self, con):
        con.execute(
            "UPDATE amostras SET den1_ct=%s, den2_ct=%s, "
            "data_resultado=CURRENT_TIMESTAMP WHERE chave=%s",
            (-1.0, 24.3, "D2/25"),
        )
        con.commit()

        linha = _df(con).set_index("Número").loc[2]
        assert pd.isna(linha["DEN1 (Ct)"])
        assert float(linha["DEN2 (Ct)"]) == 24.3
        assert linha["Sorotipo"] == "DENV-2"


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
