"""Testes da camada de UI que não exigem servidor.

Cobrem duas coisas que os testes de src/resultados.py NÃO pegam:

1. O CONTRATO com a API do NiceGUI. O import de resultados quebrou em produção
   porque o handler usava `evento.content` (API antiga) enquanto o NiceGUI 3.x
   entrega `evento.file`. Um teste puro de parsing nunca veria isso — a falha
   está na cola, não na lógica.
2. As funções puras de app.py (colunas, badges, mapeamento de linha), que hoje
   são derivadas de db.ETAPAS_DEF e precisam continuar coerentes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect

import pytest

from src import app, db


class TestContratoUploadNiceGUI:
    """Amarra o código à API real do ui.upload — quebra no upgrade, não em produção."""

    def test_evento_de_upload_expoe_file(self):
        from nicegui import events

        campos = {f.name for f in dataclasses.fields(events.UploadEventArguments)}
        assert "file" in campos, (
            "UploadEventArguments mudou: o handler em app.py lê evento.file"
        )

    def test_fileupload_tem_name_e_read_async(self):
        from nicegui.elements.upload_files import FileUpload

        campos = {f.name for f in dataclasses.fields(FileUpload)}
        assert "name" in campos, "app.py usa evento.file.name"
        assert inspect.iscoroutinefunction(FileUpload.read), (
            "FileUpload.read é async — o handler precisa dar await"
        )

    def test_handler_de_upload_e_async(self):
        """O handler dá await em file.read(), logo precisa ser assíncrono."""
        fonte = inspect.getsource(app.App.abrir_dialogo_resultados)
        assert "async def ao_subir" in fonte
        assert "await evento.file.read()" in fonte

    def test_le_planilha_do_objeto_real_do_nicegui(self):
        """Percorre o caminho do handler com o FileUpload de verdade.

        Vai além de checar a assinatura: constrói o mesmo objeto que o NiceGUI
        entrega em `evento.file`, faz o await e alimenta o parser. É o teste que
        teria pego a troca de `evento.content` por `evento.file`.
        """
        import asyncio
        import io

        import pandas as pd
        from nicegui.elements.upload_files import SmallFileUpload

        buf = io.BytesIO()
        pd.DataFrame({"NI": ["D1/25"], "DEN2": [24.3]}).to_excel(buf, index=False)
        arquivo = SmallFileUpload(
            name="r.xlsx", content_type="application/vnd.ms-excel", _data=buf.getvalue()
        )

        conteudo = asyncio.run(arquivo.read())
        df = app.resultados.ler_arquivo(conteudo, arquivo.name)
        assert list(df.columns) == ["ni", "den2_ct"]


class TestColunasDaGrade:
    """O índice da coluna 'Fase' é derivado; se errar, a grade mostra HTML cru."""

    @pytest.mark.parametrize("com_motivo", [False, True])
    @pytest.mark.parametrize("com_resultado", [False, True])
    def test_indice_da_fase_aponta_para_a_fase(self, com_motivo, com_resultado):
        cols, idx = app._colunas(com_motivo=com_motivo, com_resultado=com_resultado)
        assert cols[idx]["field"] == "fase"

    def test_colunas_de_ct_so_com_resultado(self):
        sem, _ = app._colunas(com_resultado=False)
        com, _ = app._colunas(com_resultado=True)
        campos_sem = {c["field"] for c in sem}
        campos_com = {c["field"] for c in com}
        assert not (set(db.COLUNAS_CT) & campos_sem)
        assert set(db.COLUNAS_CT) <= campos_com
        assert "sorotipo" in campos_com

    def test_motivo_so_na_aba_de_rejeitadas(self):
        sem, _ = app._colunas(com_motivo=False)
        assert "motivo" not in {c["field"] for c in sem}

    def test_campos_da_grade_batem_com_a_linha(self):
        """Toda coluna declarada precisa existir no dict que alimenta o AG-Grid."""
        linha = _linha_falsa()
        dados = app._linha_para_dict(linha)
        cols, _ = app._colunas(com_motivo=True, com_resultado=True)
        for coluna in cols:
            assert coluna["field"] in dados, f"coluna '{coluna['field']}' sem dado"


class TestBadgesEAbas:
    def test_badge_de_todas_as_fases(self):
        for fase in db.FASES:
            html = app._badge_html(fase)
            assert db.LABEL_FASE[fase] in html and db.COR_FASE[fase] in html

    def test_abas_cobrem_todas_as_etapas(self):
        fases_das_abas = {fase for fase, _ in app._ABAS}
        assert set(db.ETAPAS) <= fases_das_abas
        assert db.FASE_REJEITADA in fases_das_abas

    def test_tab_fase_e_inverso_de_abas(self):
        for fase, rotulo in app._ABAS:
            assert app._TAB_FASE[rotulo] == fase

    def test_proxima_etapa_encadeia_o_fluxo(self):
        assert app._PROXIMA_ETAPA[app._FASE_GERAL] == db.ETAPAS[0]
        for i, etapa in enumerate(db.ETAPAS_DEF[:-1]):
            assert app._PROXIMA_ETAPA[etapa.chave] == db.ETAPAS[i + 1]
        # A última etapa não avança para lugar nenhum.
        assert db.ETAPAS[-1] not in app._PROXIMA_ETAPA

    def test_cards_nao_incluem_a_geral(self):
        assert app._FASE_GERAL not in app._FASES_COM_CARD
        assert set(app._FASES_COM_CARD) <= set(db.FASES)


class TestMapaExtracaoNaUI:
    def test_botao_e_exclusivo_da_aba_coletadas(self):
        fonte = inspect.getsource(app.FaseTab._montar)
        assert '"Gerar Extração"' in fonte
        assert "self.fase == db.ETAPAS[0]" in fonte

    def test_gerar_mapa_nao_avanca_fase(self):
        fonte = inspect.getsource(app.App._baixar_mapa_extracao)
        assert "extracao.gerar_mapa_extracao" in fonte
        assert "ui.download" in fonte
        assert "avancar_fase" not in fonte
        assert "registrar_evento" not in fonte

    def test_selecao_vazia_exibe_aviso_sem_abrir_dialogo(self, monkeypatch):
        class GridVazia:
            async def get_selected_rows(self):
                return []

        avisos = []
        monkeypatch.setattr(app.ui, "notify", lambda mensagem, **kwargs: avisos.append(mensagem))
        instancia = object.__new__(app.App)
        tab = type("Tab", (), {"grid": GridVazia()})()

        asyncio.run(instancia.abrir_dialogo_extracao(tab))

        assert avisos == ["Selecione ao menos uma amostra."]

    def test_download_usa_xlsx_e_nao_precisa_de_conexao_com_banco(self, monkeypatch):
        class Dialogo:
            fechado = False

            def close(self):
                self.fechado = True

        downloads = []
        avisos = []
        monkeypatch.setattr(app.ui, "download", lambda *args: downloads.append(args))
        monkeypatch.setattr(app.ui, "notify", lambda mensagem, **kwargs: avisos.append(mensagem))
        instancia = object.__new__(app.App)
        dialogo = Dialogo()

        instancia._baixar_mapa_extracao(
            dialogo,
            [app.extracao.AmostraExtracao("D447/26", 447, 2026)],
            "2026-08-13",
            1,
            "Guilherme",
        )

        assert dialogo.fechado is True
        assert len(downloads) == 1
        conteudo, nome, media = downloads[0]
        assert conteudo.startswith(b"PK")
        assert nome.endswith("13_08_2026.xlsx")
        assert "spreadsheetml.sheet" in media
        assert avisos == ["Mapa DENV130826-1 gerado com 1 amostra(s)."]


class TestFormatacao:
    def test_ct_nulo_vira_vazio(self):
        assert app._ct_para_display(None) == ""

    def test_ct_com_uma_casa(self):
        assert app._ct_para_display(24.3) == "24.3"
        assert app._ct_para_display(20) == "20.0"
        # NUMERIC do PostgreSQL volta como Decimal — precisa formatar igual.
        from decimal import Decimal
        assert app._ct_para_display(Decimal("28.40")) == "28.4"


def _linha_falsa() -> dict:
    """Linha de banco mínima, com todas as colunas que _linha_para_dict lê."""
    return {
        "chave": "D1/25", "ni_original": "D1/25", "numero_sequencial": 1,
        "ano_verdade": 2025, "municipio": "PORTO ALEGRE", "data_coleta": "2025-03-01",
        "data_sintomas": None, "caso": "Confirmado", "motivo_rejeicao": None,
        "flags": "", "n_origem": 1, "rejeitada": 0, "data_resultado": None,
        **{e.chave: 0 for e in db.ETAPAS_DEF},
        **{c: None for c in db.COLUNAS_CT},
        **{c: None for c in db.COLUNAS_CI},
    }
