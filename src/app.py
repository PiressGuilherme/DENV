"""UI NiceGUI — tracker de reprocesso de dengue, organizado por fases (kanban).

Fase 3 do CLAUDE.md, já com o fluxo de trabalho por abas embutido (decisão do
usuário). Abas:

    - Geral: TODAS as amostras, na ordenação canônica (Seção 3.3), com badge da
      fase atual. Visão de auditoria/busca. Daqui marca-se "Coletada" em lote;
      amostras que já entraram no fluxo (qualquer status) NÃO podem reentrar.
    - Coletadas / Extraídas / PCR feito: recortes por fase. Cada aba avança a
      etapa seguinte em lote, com AVANÇO ESTRITO (bloqueia fora de ordem), e
      permite RETROCEDER (desmarcar).

Toda ação persiste no SQLite, grava evento de auditoria e atualiza as grades +
contadores. A lógica de fase mora em db.py; aqui é só a casca de UI.

Uso:
    python -m src.app
"""

from __future__ import annotations

from typing import Optional

from nicegui import ui

from src import db

# Etapa "alvo" de cada aba (o botão de avanço marca esta etapa).
# Geral marca 'coletada'; cada aba de fase marca a PRÓXIMA etapa.
_PROXIMA_ETAPA = {
    "geral": "coletada",
    "pendente": "coletada",
    "coletada": "extraida",
    "extraida": "pcr_feito",
}

# Rótulos PT-BR das fases (badge/coluna).
_LABEL_FASE = {
    "pendente": "Pendente",
    "coletada": "Coletada",
    "extraida": "Extraída",
    "pcr_feito": "PCR feito",
}

# Cor do badge por fase (classes Tailwind do NiceGUI/Quasar).
_COR_FASE = {
    "pendente": "grey",
    "coletada": "blue",
    "extraida": "amber",
    "pcr_feito": "green",
}

_ETAPA_LABEL = {"coletada": "Coletada", "extraida": "Extraída", "pcr_feito": "PCR feito"}


def _fase_da_linha(r) -> str:
    """Deriva a fase de uma linha (espelha db.FASES; partição completa)."""
    if r["pcr_feito"]:
        return "pcr_feito"
    if r["extraida"]:
        return "extraida"
    if r["coletada"]:
        return "coletada"
    return "pendente"


# Cor (hex) do badge por fase, para o HTML inline da célula.
_HEX_FASE = {
    "pendente": "#9e9e9e",
    "coletada": "#2196f3",
    "extraida": "#ff9800",
    "pcr_feito": "#4caf50",
}


def _badge_html(fase: str) -> str:
    """HTML do badge da fase (renderizado via html_columns do NiceGUI)."""
    return (
        f'<span style="padding:2px 8px;border-radius:10px;color:white;'
        f'font-size:11px;background:{_HEX_FASE[fase]}">{_LABEL_FASE[fase]}</span>'
    )


def _linha_para_dict(r) -> dict:
    """Converte uma Row do SQLite no dict que o AG-Grid consome."""
    fase = _fase_da_linha(r)
    return {
        "chave": r["chave"],                    # usada como rowId
        "ni": r["ni_original"] or r["chave"],   # display
        "numero": r["numero_sequencial"],       # numérico p/ ordenação nativa
        "ano": r["ano_verdade"],
        "municipio": r["municipio"] or "",
        "data_coleta": r["data_coleta"] or "",
        "caso": r["caso"] or "",
        "fase": _badge_html(fase),              # HTML (ver _COL_FASE_IDX em html_columns)
        "flags": r["flags"] or "",
        "n_origem": r["n_origem"],
    }


# Índice (0-based) da coluna "Fase" — registrado como html_column na grade.
_COL_FASE_IDX = 6


def _colunas() -> list[dict]:
    # Checkbox de seleção: configurado via rowSelection (API v33+), não por colDef.
    # Larguras explícitas (sem 'flex') para não conflitar com auto_size_columns.
    return [
        {"headerName": "NI", "field": "ni", "filter": True, "width": 120,
         "pinned": "left"},
        {"headerName": "Número", "field": "numero", "type": "numericColumn", "width": 110},
        {"headerName": "Ano", "field": "ano", "width": 90},
        {"headerName": "Município", "field": "municipio", "filter": True, "width": 240},
        {"headerName": "Data Coleta", "field": "data_coleta", "width": 130},
        {"headerName": "Caso", "field": "caso", "width": 120},
        {"headerName": "Fase", "field": "fase", "width": 130},  # idx 6: html_columns
        {"headerName": "Flags", "field": "flags", "filter": True, "width": 260},
        {"headerName": "Nº origem", "field": "n_origem", "type": "numericColumn",
         "width": 110},
    ]


class FaseTab:
    """Uma aba: grade + barra de ações de avanço/retrocesso para uma fase."""

    def __init__(self, app: "App", fase: str):
        self.app = app
        self.fase = fase  # "geral" | "pendente" | "coletada" | "extraida" | "pcr_feito"
        self.grid: Optional[ui.aggrid] = None
        self._montar()

    def _montar(self) -> None:
        etapa = _PROXIMA_ETAPA.get(self.fase)
        with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
            if etapa:
                ui.button(
                    f"Marcar {_ETAPA_LABEL[etapa]}",
                    icon="arrow_forward",
                    on_click=lambda: self.app.avancar(self, etapa),
                ).props("color=primary")
            # Retroceder: só nas abas de fase concreta (não na Geral nem Pendente).
            if self.fase in ("coletada", "extraida", "pcr_feito"):
                ui.button(
                    f"Desmarcar {_LABEL_FASE[self.fase]}",
                    icon="undo",
                    on_click=lambda: self.app.retroceder(self, self.fase),
                ).props("color=negative outline")
            ui.space()
            self.label_contagem = ui.label().classes("text-grey-7")

        dados = self._carregar_dados()
        self.grid = ui.aggrid({
            "columnDefs": _colunas(),
            # API de seleção do AG-Grid v33+ (checkboxSelection no colDef foi removido):
            "rowSelection": {
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckbox": True,
                "enableClickSelection": False,
            },
            "defaultColDef": {"sortable": True, "resizable": True},
            ":getRowId": "params => params.data.chave",
            "rowData": dados,  # já com dados no 1º paint (evita grade em branco)
        }, html_columns=[_COL_FASE_IDX], auto_size_columns=False).classes(
            "w-full"
        ).style("height: 65vh")
        self.label_contagem.text = f"{len(dados)} amostra(s)"

    def where(self) -> Optional[str]:
        if self.fase == "geral":
            return None
        return db.where_por_fase(self.fase)

    def _carregar_dados(self) -> list[dict]:
        rows = db.listar_amostras(self.app.con, where=self.where())
        return [_linha_para_dict(r) for r in rows]

    def recarregar(self) -> None:
        dados = self._carregar_dados()
        self.grid.options["rowData"] = dados
        self.grid.update()
        self.label_contagem.text = f"{len(dados)} amostra(s)"


class App:
    def __init__(self, db_path=None):
        self.con = db.init_db(db_path) if db_path else db.init_db()
        self.tabs: dict[str, FaseTab] = {}
        self._cards: dict[str, ui.label] = {}

    # -- helpers de seleção/ação ------------------------------------------- #
    async def _chaves_selecionadas(self, tab: FaseTab) -> list[str]:
        rows = await tab.grid.get_selected_rows()
        return [r["chave"] for r in rows]

    async def avancar(self, tab: FaseTab, etapa: str) -> None:
        chaves = await self._chaves_selecionadas(tab)
        if not chaves:
            ui.notify("Selecione ao menos uma amostra.", type="warning")
            return
        # Na Geral, amostras que já têm status não reentram: filtra elegíveis.
        if tab.fase == "geral" and etapa == "coletada":
            elegiveis, ignoradas = self._filtrar_nao_coletadas(chaves)
            if ignoradas:
                ui.notify(
                    f"{ignoradas} já no fluxo — ignorada(s). "
                    f"{len(elegiveis)} marcada(s) como Coletada.",
                    type="info",
                )
            chaves = elegiveis
            if not chaves:
                return
        try:
            n = db.avancar_fase(self.con, chaves, etapa)
        except db.TransicaoInvalida as e:
            ui.notify(str(e), type="negative")
            return
        ui.notify(f"{n} amostra(s) → {_ETAPA_LABEL[etapa]}.", type="positive")
        self.refresh()

    async def retroceder(self, tab: FaseTab, etapa: str) -> None:
        chaves = await self._chaves_selecionadas(tab)
        if not chaves:
            ui.notify("Selecione ao menos uma amostra.", type="warning")
            return
        n = db.retroceder_fase(self.con, chaves, etapa)
        ui.notify(f"{n} amostra(s) retrocedida(s) de {_LABEL_FASE[etapa]}.", type="positive")
        self.refresh()

    def _filtrar_nao_coletadas(self, chaves: list[str]) -> tuple[list[str], int]:
        ph = ",".join("?" * len(chaves))
        ja = {
            row[0]
            for row in self.con.execute(
                f"SELECT chave FROM amostras WHERE chave IN ({ph}) AND coletada = 1",
                chaves,
            ).fetchall()
        }
        elegiveis = [c for c in chaves if c not in ja]
        return elegiveis, len(ja)

    # -- render ------------------------------------------------------------ #
    def refresh(self) -> None:
        for tab in self.tabs.values():
            tab.recarregar()
        cont = db.contagens_por_fase(self.con)
        self._cards["total"].text = str(cont["total"])
        self._cards["coletada"].text = str(cont["coletada"])
        self._cards["extraida"].text = str(cont["extraida"])
        self._cards["pcr_feito"].text = str(cont["pcr_feito"])

    def _card(self, titulo: str, chave: str, cor: str) -> None:
        with ui.card().classes("items-center").style(f"border-top: 4px solid {cor}"):
            self._cards[chave] = ui.label("0").classes("text-2xl text-bold")
            ui.label(titulo).classes("text-grey-7 text-sm")

    def construir(self) -> None:
        ui.label("Reprocesso Dengue — LACEN-RS").classes("text-h5 q-mb-sm")

        with ui.row().classes("gap-4 q-mb-md"):
            self._card("Total", "total", "#607d8b")
            self._card("Coletadas", "coletada", "#2196f3")
            self._card("Extraídas", "extraida", "#ff9800")
            self._card("PCR feito", "pcr_feito", "#4caf50")

        with ui.tabs().classes("w-full") as tabs:
            t_geral = ui.tab("Geral")
            t_col = ui.tab("Coletadas")
            t_ext = ui.tab("Extraídas")
            t_pcr = ui.tab("PCR feito")

        with ui.tab_panels(tabs, value=t_geral).classes("w-full"):
            with ui.tab_panel(t_geral):
                self.tabs["geral"] = FaseTab(self, "geral")
            with ui.tab_panel(t_col):
                self.tabs["coletada"] = FaseTab(self, "coletada")
            with ui.tab_panel(t_ext):
                self.tabs["extraida"] = FaseTab(self, "extraida")
            with ui.tab_panel(t_pcr):
                self.tabs["pcr_feito"] = FaseTab(self, "pcr_feito")

        self.refresh()
        if db.contar(self.con) == 0:
            ui.notify(
                "Banco vazio — rode 'python -m src.importer' para popular.",
                type="warning",
                timeout=0,
            )


def main() -> None:
    app = App()

    @ui.page("/")
    def index():
        app.construir()

    ui.run(title="Reprocesso Dengue", reload=False, port=8080)


# NiceGUI executa o módulo; o guard padrão do framework é __mp_main__.
if __name__ in {"__main__", "__mp_main__"}:
    main()
