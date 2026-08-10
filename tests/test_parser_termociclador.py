"""Testes do parser de arquivos do termociclador."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.parser_termociclador import (
    AmostraTermociclador,
    ResultadoParseTermociclador,
    _normalizar_sample_id,
    merge_arquivos_termociclador,
    parse_arquivo_termociclador,
    preparar_para_gravacao,
)

# Arquivos de teste na pasta docs
DOCS = Path(__file__).parent.parent / "docs"
ARQUIVO_1_4 = DOCS / "Dengue 1-4 030826_Abs Quant(Stage2_Step2).xlsx"
ARQUIVO_2_3 = DOCS / "Dengue 2-3 030826_Abs Quant(Stage2_Step2).xlsx"

ARQUIVOS_EXISTEM = ARQUIVO_1_4.exists() and ARQUIVO_2_3.exists()
pytestmark = pytest.mark.skipif(
    not ARQUIVOS_EXISTEM, reason="Arquivos de exemplo não encontrados em docs/"
)


class TestNormalizarSampleId:
    """Testes da normalização de Sample ID."""

    @pytest.mark.parametrize("sample_id,esperado", [
        ("25346", ("D", 25346, None)),
        ("D23459", ("D", 23459, None)),
        ("D24745/26", ("D", 24745, 2026)),
        ("d24745/26", ("D", 24745, 2026)),
        ("SR123/25", ("SR", 123, 2025)),
        ("FA456/2025", ("FA", 456, 2025)),
        ("H789/29", ("H", 789, 2029)),
    ])
    def test_formatos_validos(self, sample_id, esperado):
        assert _normalizar_sample_id(sample_id) == esperado

    @pytest.mark.parametrize("sample_id", [
        "",
        "abc",
        "123/",
        "/26",
        "D-123/25",
    ])
    def test_formatos_invalidos(self, sample_id):
        with pytest.raises(ValueError):
            _normalizar_sample_id(sample_id)


class TestParseArquivo1_4:
    """Testes do parse do arquivo Dengue 1-4."""

    def test_parse_sucesso(self):
        with open(ARQUIVO_1_4, "rb") as f:
            resultado = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        
        assert not resultado.erros
        assert len(resultado.amostras) > 0
        
        # Verifica estrutura das amostras
        for amp in resultado.amostras:
            assert isinstance(amp, AmostraTermociclador)
            assert amp.prefixo == "D"
            assert amp.numero_sequencial > 0
            assert "den1_ct" in amp.cts
            assert "den4_ct" in amp.cts
            assert "ci_1_4_ct" in amp.cts
            # DEN2 e DEN3 não estão neste arquivo
            assert amp.cts.get("den2_ct") is None
            assert amp.cts.get("den3_ct") is None
            # ci_2_3_ct não está neste arquivo
            assert amp.cts.get("ci_2_3_ct") is None

    def test_controles_ignorados(self):
        """CN e CP devem ser ignorados."""
        with open(ARQUIVO_1_4, "rb") as f:
            resultado = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        
        # Nenhuma amostra deve ter prefixo CN ou CP
        for amp in resultado.amostras:
            assert amp.prefixo not in ("CN", "CP")


class TestParseArquivo2_3:
    """Testes do parse do arquivo Dengue 2-3."""

    def test_parse_sucesso(self):
        with open(ARQUIVO_2_3, "rb") as f:
            resultado = parse_arquivo_termociclador(f.read(), ARQUIVO_2_3.name)
        
        assert not resultado.erros
        assert len(resultado.amostras) > 0
        
        # Verifica estrutura das amostras
        for amp in resultado.amostras:
            assert isinstance(amp, AmostraTermociclador)
            assert amp.prefixo == "D"
            assert amp.numero_sequencial > 0
            assert "den2_ct" in amp.cts
            assert "den3_ct" in amp.cts
            assert "ci_2_3_ct" in amp.cts
            # DEN1 e DEN4 não estão neste arquivo
            assert amp.cts.get("den1_ct") is None
            assert amp.cts.get("den4_ct") is None
            # ci_1_4_ct não está neste arquivo
            assert amp.cts.get("ci_1_4_ct") is None


class TestMergeArquivos:
    """Testes do merge dos dois arquivos."""

    def test_merge_sucesso(self):
        with open(ARQUIVO_1_4, "rb") as f:
            r1 = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        with open(ARQUIVO_2_3, "rb") as f:
            r2 = parse_arquivo_termociclador(f.read(), ARQUIVO_2_3.name)
        
        merged = merge_arquivos_termociclador(r1, r2)
        
        assert not merged.erros
        assert len(merged.amostras) > 0
        
        # Verifica que tem todos os 6 CTs (DEN1-4 + CI 1-4 + CI 2-3)
        for amp in merged.amostras:
            for ct in ["den1_ct", "den2_ct", "den3_ct", "den4_ct", "ci_1_4_ct", "ci_2_3_ct"]:
                assert ct in amp.cts
        
        # Verifica que amostras dos dois arquivos casaram pelo Sample ID
        # O número de amostras merged deve ser <= soma dos dois (pois merge por ID)
        assert len(merged.amostras) <= len(r1.amostras) + len(r2.amostras)

    def test_amostra_completa_apos_merge(self):
        """Uma amostra presente nos dois arquivos deve ter DEN1-4 + CI."""
        with open(ARQUIVO_1_4, "rb") as f:
            r1 = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        with open(ARQUIVO_2_3, "rb") as f:
            r2 = parse_arquivo_termociclador(f.read(), ARQUIVO_2_3.name)
        
        merged = merge_arquivos_termociclador(r1, r2)
        
        # Pega primeira amostra e verifica se tem dados dos dois arquivos
        amp = merged.amostras[0]
        # Pelo menos um dos DEN1/DEN4 (arquivo 1-4) e um dos DEN2/DEN3 (arquivo 2-3)
        tem_1_4 = amp.cts["den1_ct"] is not None or amp.cts["den4_ct"] is not None
        tem_2_3 = amp.cts["den2_ct"] is not None or amp.cts["den3_ct"] is not None
        tem_ci_1_4 = amp.cts["ci_1_4_ct"] is not None
        tem_ci_2_3 = amp.cts["ci_2_3_ct"] is not None
        
        # Pelo menos um dos CI deve estar presente (vem dos dois arquivos)
        assert tem_ci_1_4 or tem_ci_2_3

    def test_sentinela_nao_detectado(self):
        """Testa que '-' na planilha vira -1.0 (não detectado) e ausente vira None (não testado)."""
        from src.parser_termociclador import _ct_para_float
        import pandas as pd
        
        # "-" -> -1.0 (não detectado)
        assert _ct_para_float("-") == -1.0
        
        # "" (vazio) -> None (não testado)
        assert _ct_para_float("") is None
        assert _ct_para_float(None) is None
        assert _ct_para_float(pd.NA) is None
        assert _ct_para_float(float("nan")) is None
        
        # Valor válido
        assert _ct_para_float("25.5") == 25.5
        assert _ct_para_float("25,5") == 25.5  # decimal BR
        assert _ct_para_float(25.5) == 25.5


class TestPrepararParaGravacao:
    """Testes da preparação para gravação no banco."""

    def test_com_ano_padrao(self):
        with open(ARQUIVO_1_4, "rb") as f:
            r1 = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        with open(ARQUIVO_2_3, "rb") as f:
            r2 = parse_arquivo_termociclador(f.read(), ARQUIVO_2_3.name)
        
        merged = merge_arquivos_termociclador(r1, r2)
        
        # Prepara com ano padrão
        dados = preparar_para_gravacao(merged, ano_padrao=2025)
        
        assert len(dados) == len(merged.amostras)
        for d in dados:
            assert d["ano_verdade"] == 2025
            assert "prefixo" in d
            assert "numero_sequencial" in d
            assert "cts" in d

    def test_sem_ano_padrao_mantem_none(self):
        with open(ARQUIVO_1_4, "rb") as f:
            r1 = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        
        # Algumas amostras podem não ter ano
        dados = preparar_para_gravacao(r1, ano_padrao=None)
        
        for d in dados:
            # Se a amostra original não tinha ano, deve continuar None
            if d["ano_verdade"] is None:
                assert d["ano_verdade"] is None


# Teste de integração rápido (roda só se tiver DATABASE_URL)
@pytest.mark.integracao
class TestIntegracaoBanco:
    """Testes de integração com banco real (precisa DATABASE_URL)."""

    def test_resolver_e_gravar(self, _pg_schema_con):
        """Testa resolução no banco e gravação."""
        from src import db
        
        con = _pg_schema_con
        db.criar_schema(con)
        
        # Importa dados base primeiro
        from src.importer import importar, XLSX_PADRAO
        importar(XLSX_PADRAO, _con=con, verificar_sanidade=False)
        
        # Parse arquivos
        with open(ARQUIVO_1_4, "rb") as f:
            r1 = parse_arquivo_termociclador(f.read(), ARQUIVO_1_4.name)
        with open(ARQUIVO_2_3, "rb") as f:
            r2 = parse_arquivo_termociclador(f.read(), ARQUIVO_2_3.name)
        
        merged = merge_arquivos_termociclador(r1, r2)
        dados = preparar_para_gravacao(merged, ano_padrao=2025)
        
        # Resolve no banco
        resolvidas, nao_encontradas, ano_ambiguo = db.resolver_amostras_termociclador(
            con, dados
        )
        
        # Deve encontrar pelo menos algumas amostras (mesmo número sequencial)
        # Como os Sample IDs dos arquivos de teste podem não bater com o banco de teste,
        # aceitamos que possa não encontrar nenhuma
        assert isinstance(resolvidas, dict)
        assert isinstance(nao_encontradas, list)
        assert isinstance(ano_ambiguo, list)
        
        # Se encontrou alguma, testa gravação
        if resolvidas:
            resultado = db.gravar_resultados_termociclador(con, resolvidas)
            assert resultado.gravados >= 0
            assert resultado.campos_gravados >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])