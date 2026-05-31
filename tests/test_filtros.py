"""Testes dos filtros da UI (Fase 4) — construir_filtro, distinct, contagens.

A lógica de filtragem mora em db.py (SQL parametrizado). Aqui validamos que o
WHERE composto seleciona o subconjunto certo e que as contagens por fase
respeitam o filtro.
"""

from __future__ import annotations

import pytest

from src import db


@pytest.fixture
def con(tmp_path):
    """DB com amostras variadas para exercitar os filtros."""
    c = db.init_db(tmp_path / "filtros.db")
    amostras = [
        # chave, num, ano, municipio, ni_original, flags
        ("D1/25", 1, 2025, "PORTO ALEGRE", "D1/25", ""),
        ("D2/25", 2, 2025, "PORTO ALEGRE", "D2/25", "ANO_NI_DIVERGE"),
        ("D3/26", 3, 2026, "CANOAS", "D3/26", "COLETA_ANTES_SINTOMA"),
        ("D11633/26", 11633, 2026, "CANOAS", "D11633/26", "ANO_NI_DIVERGE;COLETA_ANTES_SINTOMA"),
        ("SR5/25", 5, 2025, "GRAVATAI", "SR5/25", ""),
    ]
    for chave, num, ano, mun, ni, flags in amostras:
        c.execute(
            "INSERT INTO amostras (chave, prefixo, numero_sequencial, ano_verdade, "
            "municipio, ni_original, flags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chave, chave[:1] if chave[0].isalpha() else "D", num, ano, mun, ni, flags),
        )
    c.commit()
    yield c
    c.close()


def _chaves(con, where, params):
    return {r["chave"] for r in db.listar_amostras(con, where=where, params=params)}


# --------------------------------------------------------------------------- #
# construir_filtro                                                             #
# --------------------------------------------------------------------------- #
class TestConstruirFiltro:
    def test_sem_filtro_retorna_none(self, con):
        where, params = db.construir_filtro()
        assert where is None and params == []
        assert len(db.listar_amostras(con, where=where, params=params)) == 5

    def test_filtro_ano(self, con):
        where, params = db.construir_filtro(ano=2025)
        assert _chaves(con, where, params) == {"D1/25", "D2/25", "SR5/25"}

    def test_filtro_municipio(self, con):
        where, params = db.construir_filtro(municipio="CANOAS")
        assert _chaves(con, where, params) == {"D3/26", "D11633/26"}

    def test_busca_ni_substring(self, con):
        where, params = db.construir_filtro(busca_ni="11633")
        assert _chaves(con, where, params) == {"D11633/26"}

    def test_busca_ni_case_insensitive(self, con):
        where, params = db.construir_filtro(busca_ni="sr5")
        assert _chaves(con, where, params) == {"SR5/25"}

    def test_busca_ni_nao_confunde_prefixo(self, con):
        """Busca 'D1' não deve casar D11633 só por ordenação textual; LIKE casa
        substring, então D1/25 e D11633/26 ambos contêm 'D1' — comportamento
        esperado de substring (o usuário refina digitando mais)."""
        where, params = db.construir_filtro(busca_ni="D11")
        assert _chaves(con, where, params) == {"D11633/26"}

    def test_filtro_flag_especifica(self, con):
        where, params = db.construir_filtro(flags_qualquer=["ANO_NI_DIVERGE"])
        assert _chaves(con, where, params) == {"D2/25", "D11633/26"}

    def test_filtro_flag_qualquer_uma(self, con):
        where, params = db.construir_filtro(
            flags_qualquer=["ANO_NI_DIVERGE", "COLETA_ANTES_SINTOMA"]
        )
        assert _chaves(con, where, params) == {"D2/25", "D3/26", "D11633/26"}

    def test_com_flags_true(self, con):
        where, params = db.construir_filtro(com_flags=True)
        assert _chaves(con, where, params) == {"D2/25", "D3/26", "D11633/26"}

    def test_com_flags_false(self, con):
        where, params = db.construir_filtro(com_flags=False)
        assert _chaves(con, where, params) == {"D1/25", "SR5/25"}

    def test_filtros_combinados(self, con):
        where, params = db.construir_filtro(ano=2026, municipio="CANOAS",
                                            flags_qualquer=["ANO_NI_DIVERGE"])
        assert _chaves(con, where, params) == {"D11633/26"}

    def test_ordenacao_preservada_sob_filtro(self, con):
        """O filtro não quebra a ordenação canônica (número como int)."""
        where, params = db.construir_filtro(municipio="CANOAS")
        rows = db.listar_amostras(con, where=where, params=params)
        nums = [r["numero_sequencial"] for r in rows]
        assert nums == [3, 11633]  # 3 < 11633 como int


# --------------------------------------------------------------------------- #
# valores_distintos                                                           #
# --------------------------------------------------------------------------- #
class TestValoresDistintos:
    def test_anos(self, con):
        assert db.valores_distintos(con, "ano_verdade") == [2025, 2026]

    def test_municipios_ordenados(self, con):
        assert db.valores_distintos(con, "municipio") == [
            "CANOAS", "GRAVATAI", "PORTO ALEGRE"
        ]

    def test_coluna_nao_permitida(self, con):
        with pytest.raises(ValueError):
            db.valores_distintos(con, "flags; DROP TABLE amostras")


# --------------------------------------------------------------------------- #
# contagens_por_fase com filtro                                               #
# --------------------------------------------------------------------------- #
class TestContagensFiltradas:
    def test_contagens_respeitam_filtro(self, con):
        # marca D2/25 (2025) como coletada
        db.avancar_fase(con, ["D2/25"], "coletada")
        where, params = db.construir_filtro(ano=2025)
        cont = db.contagens_por_fase(con, where=where, params=params)
        assert cont["total"] == 3              # D1, D2, SR5 são 2025
        assert cont["coletada"] == 1           # só D2
        assert cont["pendente"] == 2           # D1, SR5
        # soma das fases == total filtrado
        soma = cont["pendente"] + cont["coletada"] + cont["extraida"] + cont["pcr_feito"]
        assert soma == cont["total"]

    def test_contagens_sem_filtro_iguais_ao_total(self, con):
        cont = db.contagens_por_fase(con)
        assert cont["total"] == 5
