"""Testes do parsing/triagem do import de resultados (puros — sem banco)."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src import db, resultados
from src.resultados import ArquivoInvalido


def _xlsx(dados: dict) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(dados).to_excel(buf, index=False)
    return buf.getvalue()


def _csv(texto: str, encoding: str = "utf-8") -> bytes:
    return texto.encode(encoding)


def _banco(*linhas: dict) -> list[dict]:
    """Linhas de banco falsas com os defaults do caso feliz (em PCR feito, sem resultado)."""
    padrao = {
        "prefixo": "D", "pcr_feito": 1, "sequenciado": 0, "rejeitada": 0,
        "data_resultado": None, "ano_verdade": 2025, "ni_original": None,
    }
    return [{**padrao, **l} for l in linhas]


# --------------------------------------------------------------------------- #
# Leitura de arquivo                                                            #
# --------------------------------------------------------------------------- #
class TestLeitura:
    def test_le_xlsx(self):
        df = resultados.ler_arquivo(
            _xlsx({"NI": ["D1/25"], "DEN1": [24.3], "DEN2": [None],
                   "DEN3": [None], "DEN4": [None]}),
            "res.xlsx",
        )
        assert list(df.columns) == ["ni", *db.COLUNAS_CT]
        assert df.iloc[0]["ni"] == "D1/25"

    def test_le_csv(self):
        df = resultados.ler_arquivo(
            _csv("NI,DEN1,DEN2,DEN3,DEN4\nD1/25,24.3,,,\n"), "res.csv"
        )
        assert len(df) == 1

    def test_csv_com_ponto_e_virgula(self):
        """Excel PT-BR salva CSV com ';' — o separador é inferido."""
        df = resultados.ler_arquivo(
            _csv("NI;DEN1;DEN2;DEN3;DEN4\nD1/25;24,3;;;\n"), "res.csv"
        )
        assert len(df) == 1
        assert df.iloc[0]["ni"] == "D1/25"

    def test_csv_latin1(self):
        df = resultados.ler_arquivo(
            _csv("NI,DEN1,DEN2,DEN3,DEN4\nD1/25,24.3,,,\n", "latin-1"), "res.csv"
        )
        assert len(df) == 1

    def test_cabecalhos_variantes(self):
        """'Número Interno' e 'DENV-1' são aceitos como NI/DEN1."""
        df = resultados.ler_arquivo(
            _xlsx({"Número Interno": ["D1/25"], "DENV-1": [24.3], "den 2": [None],
                   "DEN3": [None], "DEN4": [None]}),
            "res.xlsx",
        )
        assert "ni" in df.columns and "den1_ct" in df.columns and "den2_ct" in df.columns

    def test_colunas_extras_sao_descartadas(self):
        df = resultados.ler_arquivo(
            _xlsx({"NI": ["D1/25"], "DEN1": [24.3], "Observação": ["bla"],
                   "Placa": [7]}),
            "res.xlsx",
        )
        assert list(df.columns) == ["ni", "den1_ct"]

    def test_sem_coluna_ni_falha(self):
        with pytest.raises(ArquivoInvalido, match="NI"):
            resultados.ler_arquivo(_xlsx({"DEN1": [24.3]}), "res.xlsx")

    def test_sem_coluna_sorotipo_falha(self):
        with pytest.raises(ArquivoInvalido, match="sorotipo"):
            resultados.ler_arquivo(_xlsx({"NI": ["D1/25"]}), "res.xlsx")

    def test_extensao_nao_suportada(self):
        with pytest.raises(ArquivoInvalido, match="Extensão"):
            resultados.ler_arquivo(b"qualquer", "res.pdf")

    def test_arquivo_corrompido(self):
        with pytest.raises(ArquivoInvalido):
            resultados.ler_arquivo(b"\x00\x01lixo", "res.xlsx")


# --------------------------------------------------------------------------- #
# Conversão de Ct                                                               #
# --------------------------------------------------------------------------- #
class TestCt:
    @pytest.mark.parametrize("entrada,esperado", [
        (24.3, 24.3), ("24.3", 24.3), ("24,3", 24.3), (31, 31.0), (" 18,7 ", 18.7),
    ])
    def test_valores_validos(self, entrada, esperado):
        assert resultados._como_ct(entrada) == (esperado, True)

    @pytest.mark.parametrize("entrada", [
        None, "", "  ", "-", "neg", "NEG", "Negativo", "ND", "Undetermined",
        "N/A", float("nan"), 0, "0",
    ])
    def test_nao_detectado(self, entrada):
        ct, valido = resultados._como_ct(entrada)
        assert ct is None and valido is True

    @pytest.mark.parametrize("entrada", ["abc", "24.3.5", "positivo", "??"])
    def test_invalidos(self, entrada):
        ct, valido = resultados._como_ct(entrada)
        assert valido is False

    @pytest.mark.parametrize("entrada", [-5, 51, 120])
    def test_fora_da_faixa_plausivel(self, entrada):
        """Ct fora de (0, 50] é quase sempre coluna trocada — recusa, não grava."""
        assert resultados._como_ct(entrada)[1] is False


# --------------------------------------------------------------------------- #
# Triagem                                                                       #
# --------------------------------------------------------------------------- #
class TestTriagem:
    def _plano(self, dados: dict, banco: list[dict]):
        df = resultados.ler_arquivo(_xlsx(dados), "r.xlsx")
        return resultados.montar_plano(df, banco)

    def test_caso_feliz(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [None], "DEN2": [24.3], "DEN3": [None],
             "DEN4": [None]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert len(plano.aplicaveis) == 1
        linha = plano.aplicaveis[0]
        assert linha.chave == "D1/25"
        assert linha.cts["den2_ct"] == 24.3
        assert linha.cts["den1_ct"] is None

    def test_ni_nao_encontrado(self):
        plano = self._plano(
            {"NI": ["D999/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_NAO_ENCONTRADO

    def test_ni_invalido(self):
        plano = self._plano(
            {"NI": ["sem barra"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_NI_INVALIDO

    def test_resolve_pelo_ano_de_verdade(self):
        """'D1264/25' coletada em 2026 está gravada como 'D1264/26'.

        É o caso da reclassificação 2026: casar só por string crua perderia a
        amostra silenciosamente.
        """
        plano = self._plano(
            {"NI": ["D1264/25"], "DEN1": [24.3]},
            _banco({"chave": "D1264/26", "numero_sequencial": 1264,
                    "ano_verdade": 2026}),
        )
        assert len(plano.aplicaveis) == 1
        assert plano.aplicaveis[0].chave == "D1264/26"

    def test_ni_ambiguo_nao_adivinha(self):
        """Fallback por prefixo+número com mais de um ano: reporta, não escolhe.

        NI 'D5/24' não bate chave nenhuma, e o fallback acha D5/25 e D5/26 —
        gravar o Ct na amostra errada é pior do que não gravar.
        """
        plano = self._plano(
            {"NI": ["D5/24"], "DEN1": [24.3]},
            _banco(
                {"chave": "D5/25", "numero_sequencial": 5, "ano_verdade": 2025},
                {"chave": "D5/26", "numero_sequencial": 5, "ano_verdade": 2026},
            ),
        )
        assert not plano.aplicaveis
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_AMBIGUO

    def test_chave_exata_vence_o_fallback(self):
        """Havendo chave exata, o ano homônimo não torna o NI ambíguo."""
        plano = self._plano(
            {"NI": ["D5/25"], "DEN1": [24.3]},
            _banco(
                {"chave": "D5/25", "numero_sequencial": 5, "ano_verdade": 2025},
                {"chave": "D5/26", "numero_sequencial": 5, "ano_verdade": 2026},
            ),
        )
        assert [l.chave for l in plano.aplicaveis] == ["D5/25"]

    def test_ni_duplicado_nao_confunde_barra(self):
        """'D1/25' e 'D12/5' são amostras distintas, não duplicata."""
        plano = self._plano(
            {"NI": ["D1/25", "D12/5"], "DEN1": [24.3, 25.0]},
            _banco(
                {"chave": "D1/25", "numero_sequencial": 1},
                {"chave": "D12/05", "numero_sequencial": 12, "ano_verdade": 2005},
            ),
        )
        assert len(plano.aplicaveis) == 2

    def test_fora_da_fase_pcr(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1, "pcr_feito": 0}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_FORA_DE_FASE

    def test_rejeitada_fica_fora(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1, "rejeitada": 1}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_FORA_DE_FASE

    def test_ja_tem_resultado_e_pulada(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1,
                    "data_resultado": "2026-01-01"}),
        )
        assert not plano.aplicaveis
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_JA_TEM_RESULTADO

    def test_sequenciada_ainda_aceita_resultado(self):
        """Resultado que chega depois do envio para sequenciamento é caso real."""
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1, "sequenciado": 1}),
        )
        assert len(plano.aplicaveis) == 1

    def test_sem_nenhum_ct(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": [None], "DEN2": [None], "DEN3": [None],
             "DEN4": [None]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_SEM_CT

    def test_ct_invalido_recusa_a_linha(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN1": ["abc"], "DEN2": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert not plano.aplicaveis
        assert plano.ignoradas[0].motivo.startswith(resultados.MOTIVO_CT_INVALIDO)
        assert "DEN1" in plano.ignoradas[0].motivo

    def test_ni_duplicado_no_arquivo(self):
        plano = self._plano(
            {"NI": ["D1/25", "D1/25"], "DEN1": [24.3, 25.0]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert len(plano.aplicaveis) == 1
        assert plano.aplicaveis[0].cts["den1_ct"] == 24.3  # vence a 1ª ocorrência
        assert resultados.MOTIVO_NI_DUPLICADO in plano.ignoradas[0].motivo

    def test_ni_vazio(self):
        plano = self._plano(
            {"NI": [None, "D1/25"], "DEN1": [24.3, 30.0]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert plano.ignoradas[0].motivo == resultados.MOTIVO_NI_AUSENTE

    def test_numero_da_linha_bate_com_o_excel(self):
        """Linha 2 do arquivo = 1ª após o cabeçalho, como o usuário vê."""
        plano = self._plano(
            {"NI": ["D999/25"], "DEN1": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        assert plano.ignoradas[0].linha_num == 2

    def test_agrupamento_por_motivo(self):
        plano = self._plano(
            {"NI": ["D8/25", "D9/25", "D1/25"], "DEN1": [24.3, 24.3, 24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        grupos = plano.por_motivo()
        assert len(grupos[resultados.MOTIVO_NAO_ENCONTRADO]) == 2

    def test_registros_no_formato_do_banco(self):
        plano = self._plano(
            {"NI": ["D1/25"], "DEN2": [24.3]},
            _banco({"chave": "D1/25", "numero_sequencial": 1}),
        )
        reg = plano.registros()[0]
        assert reg["chave"] == "D1/25"
        assert set(reg) == {"chave", *db.COLUNAS_CT}
        assert reg["den2_ct"] == 24.3 and reg["den1_ct"] is None


# --------------------------------------------------------------------------- #
# Sorotipo derivado                                                             #
# --------------------------------------------------------------------------- #
class TestSorotipo:
    def _linha(self, **cts):
        base = {c: None for c in db.COLUNAS_CT}
        base["data_resultado"] = cts.pop("data_resultado", "2026-01-01")
        return {**base, **cts}

    def test_um_sorotipo(self):
        assert resultados.sorotipo_de(self._linha(den2_ct=24.3)) == "DENV-2"

    def test_coinfeccao(self):
        r = self._linha(den1_ct=24.3, den2_ct=30.0)
        assert resultados.sorotipo_de(r) == "DENV-1+2"

    def test_nao_detectado(self):
        assert resultados.sorotipo_de(self._linha()) == "Não detectado"

    def test_sem_resultado_fica_vazio(self):
        """Sem data_resultado a amostra nunca foi importada — não é 'negativa'."""
        assert resultados.sorotipo_de(self._linha(data_resultado=None)) == ""


class TestCsvIgnoradas:
    def test_gera_csv_com_motivos(self):
        df = resultados.ler_arquivo(
            _xlsx({"NI": ["D999/25"], "DEN1": [24.3]}), "r.xlsx"
        )
        plano = resultados.montar_plano(df, _banco({"chave": "D1/25", "numero_sequencial": 1}))
        texto = resultados.ignoradas_para_csv(plano).decode("utf-8-sig")
        assert "D999/25" in texto and resultados.MOTIVO_NAO_ENCONTRADO in texto
