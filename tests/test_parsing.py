"""Testes de parsing e ordenação — Fase 1 (Seção 8 do CLAUDE.md).

Casos obrigatórios:
    - ordenação D1264 < D11633 (número como INTEGER, não texto);
    - D828/29 cai em 2026 pela Data da Coleta (ano-de-verdade);
    - D001/26 coletada em 2025 recebe ANO_NI_DIVERGE (virada de ano).

Mais os casos de borda observados nos dados reais (Seção 2): prefixos SR/FA/H,
caixa baixa "d", sufixos de ano de 3/4 dígitos, e NI não-parseável.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.parsing import (
    FLAG_ANO_NI_DIVERGE,
    FLAG_ANO_NI_IMPOSSIVEL,
    FLAG_COLETA_ANTES_SINTOMA,
    FLAG_SEM_DATA_COLETA,
    calcular_flags,
    chave_ordenacao,
    montar_chave,
    parse_ni,
    ano_verdade,
    reclassificar_2026,
)


# --------------------------------------------------------------------------- #
# parse_ni                                                                     #
# --------------------------------------------------------------------------- #
class TestParseNI:
    def test_ni_basico(self):
        r = parse_ni("D1264/25")
        assert r is not None
        assert r.prefixo == "D"
        assert r.numero_sequencial == 1264
        assert r.ni_ano == 2025

    def test_numero_eh_inteiro(self):
        assert isinstance(parse_ni("D1264/25").numero_sequencial, int)

    def test_prefixo_sr(self):
        r = parse_ni("SR38/26")
        assert r.prefixo == "SR"
        assert r.numero_sequencial == 38
        assert r.ni_ano == 2026

    @pytest.mark.parametrize("ni,prefixo", [
        ("FA3/25", "FA"),
        ("H318/2025", "H"),
        ("d5/26", "D"),       # caixa baixa normalizada para maiúscula
        ("499/25", "D"),       # prefixo vazio -> default D
    ])
    def test_prefixos_e_default(self, ni, prefixo):
        assert parse_ni(ni).prefixo == prefixo

    def test_espacos_ao_redor_da_barra(self):
        r = parse_ni("  D1264 / 25 ")
        assert r.prefixo == "D"
        assert r.numero_sequencial == 1264
        assert r.ni_ano == 2025

    @pytest.mark.parametrize("ni,esperado", [
        ("D1612/026", 2026),   # 3 dígitos com zero à esquerda
        ("H318/2025", 2025),   # 4 dígitos completos
        ("D1264/25", 2025),    # 2 dígitos
        ("D828/29", 2029),     # impossível, mas parseável
    ])
    def test_normalizacao_ano(self, ni, esperado):
        assert parse_ni(ni).ni_ano == esperado

    @pytest.mark.parametrize("ni", [
        None,
        "",
        "   ",
        "D3809",        # sem /ano (caso real não-parseável)
        "D28555",
        "//",
        "abc",
    ])
    def test_ni_nao_parseavel_retorna_none(self, ni):
        assert parse_ni(ni) is None


# --------------------------------------------------------------------------- #
# Ordenação — a dor central (Seção 3.3)                                        #
# --------------------------------------------------------------------------- #
class TestOrdenacao:
    def test_d1264_antes_de_d11633(self):
        """O bug do Excel A-Z: como texto, "D11633" < "D1264". Como int, não."""
        a = parse_ni("D1264/25")
        b = parse_ni("D11633/25")
        ka = chave_ordenacao(a.prefixo, a.numero_sequencial, 2025)
        kb = chave_ordenacao(b.prefixo, b.numero_sequencial, 2025)
        assert ka < kb
        # E prova de que a comparação textual estaria errada:
        assert "D11633" < "D1264"  # ordenação textual (errada) que queremos evitar

    def test_ordenacao_lista_completa(self):
        nis = ["D11633/25", "D1264/25", "D2/25", "D100/25", "D9802/25"]
        parsed = [parse_ni(n) for n in nis]
        ordenado = sorted(
            parsed, key=lambda p: chave_ordenacao(p.prefixo, p.numero_sequencial, 2025)
        )
        numeros = [p.numero_sequencial for p in ordenado]
        assert numeros == [2, 100, 1264, 9802, 11633]

    def test_ordena_por_ano_primeiro(self):
        """Ano-de-verdade tem precedência sobre o número."""
        # D9999/25 (2025) deve vir antes de D1/26 (2026), apesar do número maior.
        k_2025 = chave_ordenacao("D", 9999, 2025)
        k_2026 = chave_ordenacao("D", 1, 2026)
        assert k_2025 < k_2026

    def test_ordena_por_prefixo_dentro_do_ano(self):
        k_d = chave_ordenacao("D", 500, 2025)
        k_sr = chave_ordenacao("SR", 1, 2025)
        assert k_d < k_sr  # "D" < "SR"


# --------------------------------------------------------------------------- #
# ano_verdade (Seção 3.1)                                                      #
# --------------------------------------------------------------------------- #
class TestAnoVerdade:
    def test_coleta_vence_ni(self):
        """D828/29: NI diz 2029, mas a coleta em jan/2026 manda."""
        r = parse_ni("D828/29")
        av = ano_verdade(r.ni_ano, datetime(2026, 1, 15))
        assert av == 2026

    def test_fallback_para_ni_sem_coleta(self):
        r = parse_ni("D1264/25")
        assert ano_verdade(r.ni_ano, None) == 2025

    def test_ambos_ausentes_retorna_none(self):
        assert ano_verdade(None, None) is None

    def test_aceita_date_e_datetime(self):
        assert ano_verdade(2025, date(2026, 3, 1)) == 2026
        assert ano_verdade(2025, datetime(2026, 3, 1, 10, 30)) == 2026


# --------------------------------------------------------------------------- #
# Flags (Seção 3.4)                                                            #
# --------------------------------------------------------------------------- #
class TestFlags:
    def test_d001_26_reclassificada_para_2026_sem_flag(self):
        """Reclassificação 2026: D001/26 coletada em 30/12/2025 vira 2026 e NÃO
        recebe ANO_NI_DIVERGE (decisão posterior do usuário). Antes da regra,
        ficava em 2025 com a flag."""
        r = parse_ni("D001/26")
        coleta = datetime(2025, 12, 30)
        av = ano_verdade(
            r.ni_ano, coleta, prefixo=r.prefixo, numero_sequencial=r.numero_sequencial
        )
        assert av == 2026
        flags = calcular_flags(
            ni_ano=r.ni_ano, ano_verdade_=av, data_coleta=coleta, data_sintomas=None
        )
        assert FLAG_ANO_NI_DIVERGE not in flags

    def test_diverge_quando_regra_nao_aplica(self):
        """Fora do range da reclassificação (nº > 976), a virada de ano continua
        sinalizada: D977/26 coletada em 2025 fica 2025 + ANO_NI_DIVERGE."""
        r = parse_ni("D977/26")
        coleta = datetime(2025, 12, 30)
        av = ano_verdade(
            r.ni_ano, coleta, prefixo=r.prefixo, numero_sequencial=r.numero_sequencial
        )
        assert av == 2025
        flags = calcular_flags(
            ni_ano=r.ni_ano, ano_verdade_=av, data_coleta=coleta, data_sintomas=None
        )
        assert FLAG_ANO_NI_DIVERGE in flags

    def test_ano_ni_impossivel(self):
        """D828/29: ano do NI >= 2027 -> ANO_NI_IMPOSSIVEL, reposicionado por coleta."""
        r = parse_ni("D828/29")
        coleta = datetime(2026, 1, 15)
        av = ano_verdade(r.ni_ano, coleta)
        flags = calcular_flags(
            ni_ano=r.ni_ano, ano_verdade_=av, data_coleta=coleta, data_sintomas=None
        )
        assert FLAG_ANO_NI_IMPOSSIVEL in flags
        # também diverge, pois 2029 != 2026
        assert FLAG_ANO_NI_DIVERGE in flags

    def test_sem_data_coleta(self):
        r = parse_ni("D1264/25")
        av = ano_verdade(r.ni_ano, None)
        flags = calcular_flags(
            ni_ano=r.ni_ano, ano_verdade_=av, data_coleta=None, data_sintomas=None
        )
        assert FLAG_SEM_DATA_COLETA in flags

    def test_coleta_antes_sintoma(self):
        flags = calcular_flags(
            ni_ano=2025,
            ano_verdade_=2025,
            data_coleta=datetime(2025, 3, 1),
            data_sintomas=datetime(2025, 3, 10),
        )
        assert FLAG_COLETA_ANTES_SINTOMA in flags

    def test_amostra_limpa_sem_flags(self):
        """Coleta presente, ano bate, coleta depois do sintoma -> nenhuma flag."""
        flags = calcular_flags(
            ni_ano=2025,
            ano_verdade_=2025,
            data_coleta=datetime(2025, 3, 10),
            data_sintomas=datetime(2025, 3, 1),
        )
        assert flags == ""

    def test_multiplas_flags_separadas_por_ponto_e_virgula(self):
        flags = calcular_flags(
            ni_ano=2029,
            ano_verdade_=2026,
            data_coleta=datetime(2026, 1, 1),
            data_sintomas=datetime(2026, 1, 10),  # coleta antes do sintoma
        )
        partes = flags.split(";")
        assert FLAG_ANO_NI_DIVERGE in partes
        assert FLAG_ANO_NI_IMPOSSIVEL in partes
        assert FLAG_COLETA_ANTES_SINTOMA in partes


# --------------------------------------------------------------------------- #
# montar_chave (Seção 3.2)                                                     #
# --------------------------------------------------------------------------- #
class TestMontarChave:
    @pytest.mark.parametrize("prefixo,num,ano,esperado", [
        ("D", 1264, 2025, "D1264/25"),
        ("SR", 38, 2026, "SR38/26"),
        ("D", 1, 2025, "D1/25"),
        ("D", 828, 2026, "D828/26"),   # reposicionado pela coleta, não pelo NI
    ])
    def test_montar_chave(self, prefixo, num, ano, esperado):
        assert montar_chave(prefixo, num, ano) == esperado


# --------------------------------------------------------------------------- #
# Reclassificação 2026 (decisão posterior do usuário)                          #
# --------------------------------------------------------------------------- #
class TestReclassificacao2026:
    @pytest.mark.parametrize("prefixo,num,ni_ano,esperado", [
        ("D", 1, 2026, True),       # limite inferior
        ("D", 976, 2026, True),     # limite superior
        ("D", 500, 2026, True),     # meio
        ("D", 977, 2026, False),    # fora do range (acima)
        ("D", 0, 2026, False),      # fora do range (abaixo)
        ("D", 1, 2025, False),      # ni_ano não é 2026
        ("SR", 1, 2026, False),     # prefixo não é D
        ("D", 1, None, False),      # sem ni_ano
    ])
    def test_predicado(self, prefixo, num, ni_ano, esperado):
        assert reclassificar_2026(prefixo, num, ni_ano) is esperado

    def test_ano_verdade_forca_2026_no_grupo(self):
        # coleta em 2025, mas a regra força 2026
        assert ano_verdade(2026, datetime(2025, 12, 30), prefixo="D",
                           numero_sequencial=5) == 2026

    def test_ano_verdade_sem_args_mantem_regra_antiga(self):
        # sem prefixo/numero, a Data da Coleta vence (compatibilidade)
        assert ano_verdade(2026, datetime(2025, 12, 30)) == 2025

    def test_chave_reflete_2026(self):
        av = ano_verdade(2026, datetime(2025, 12, 30), prefixo="D", numero_sequencial=1)
        assert montar_chave("D", 1, av) == "D1/26"

    def test_ordena_apos_2025(self):
        """Reclassificada (2026) ordena depois de uma 2025 de número maior."""
        k_2025 = chave_ordenacao("D", 5000, 2025)
        k_reclass = chave_ordenacao("D", 1, 2026)
        assert k_2025 < k_reclass
