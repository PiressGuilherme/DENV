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

import os
import threading
from datetime import datetime
from typing import Optional

from nicegui import app as _nicegui_app
from nicegui import ui

from src import auth, db, export


def _startup_db() -> None:
    """Cria schema e popula o banco no primeiro boot (roda em thread — não bloqueia porta)."""
    if not db._DATABASE_URL:
        return
    try:
        con = db.init_db()   # cria schema se não existir (idempotente)
        n = db.contar(con)
        con.close()
    except Exception as e:
        print(f"[startup] Erro ao conectar ao banco: {e}", flush=True)
        return
    if n == 0:
        print("[startup] Banco vazio — importando dados do xlsx...", flush=True)
        from src.importer import importar
        importar(verificar_sanidade=False)
        print("[startup] Import concluído.", flush=True)


_nicegui_app.on_startup(
    lambda: threading.Thread(target=_startup_db, daemon=True).start()
)

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
    "rejeitada": "Rejeitada",
}

# Cor do badge por fase (classes Tailwind do NiceGUI/Quasar).
_COR_FASE = {
    "pendente": "grey",
    "coletada": "blue",
    "extraida": "amber",
    "pcr_feito": "green",
    "rejeitada": "red",
}

_ETAPA_LABEL = {"coletada": "Coletada", "extraida": "Extraída", "pcr_feito": "PCR feito"}


def _fase_da_linha(r) -> str:
    """Deriva a fase de uma linha (espelha db.FASES; partição completa)."""
    if r["rejeitada"]:
        return "rejeitada"
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
    "rejeitada": "#e53935",
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
        "data_sintomas": r["data_sintomas"] or "",
        "caso": r["caso"] or "",
        "fase": _badge_html(fase),              # HTML (ver _COL_FASE_IDX em html_columns)
        "motivo": r["motivo_rejeicao"] or "",
        "flags": r["flags"] or "",
        "n_origem": r["n_origem"],
    }


# Índice (0-based) da coluna "Fase" — registrado como html_column na grade.
_COL_FASE_IDX = 7


def _colunas(com_motivo: bool = False) -> list[dict]:
    # Checkbox de seleção: configurado via rowSelection (API v33+), não por colDef.
    # Larguras explícitas (sem 'flex') para não conflitar com auto_size_columns.
    cols = [
        {"headerName": "NI", "field": "ni", "filter": True, "width": 120,
         "pinned": "left"},
        {"headerName": "Número", "field": "numero", "type": "numericColumn", "width": 110},
        {"headerName": "Ano", "field": "ano", "width": 90},
        {"headerName": "Município", "field": "municipio", "filter": True, "width": 240},
        {"headerName": "Data Coleta", "field": "data_coleta", "width": 130},
        {"headerName": "Data 1º Sintoma", "field": "data_sintomas", "width": 140},
        {"headerName": "Caso", "field": "caso", "width": 120},
        {"headerName": "Fase", "field": "fase", "width": 130},  # idx 7: html_columns
    ]
    if com_motivo:
        cols.append({"headerName": "Motivo", "field": "motivo", "filter": True,
                     "width": 180})
    cols += [
        {"headerName": "Flags", "field": "flags", "filter": True, "width": 260},
        {"headerName": "Nº origem", "field": "n_origem", "type": "numericColumn",
         "width": 110},
    ]
    return cols


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
            # Rejeitar: só na Geral (rejeita amostras pendentes).
            if self.fase == "geral":
                ui.button(
                    "Rejeitar",
                    icon="block",
                    on_click=lambda: self.app.abrir_dialogo_rejeicao(self),
                ).props("color=negative outline")
            # Retroceder: só nas abas de fase concreta (não na Geral nem Pendente).
            if self.fase in ("coletada", "extraida", "pcr_feito"):
                ui.button(
                    f"Desmarcar {_LABEL_FASE[self.fase]}",
                    icon="undo",
                    on_click=lambda: self.app.retroceder(self, self.fase),
                ).props("color=negative outline")
            # Reverter: só na aba Rejeitadas (volta a Pendente).
            if self.fase == "rejeitada":
                ui.button(
                    "Reverter rejeição",
                    icon="undo",
                    on_click=lambda: self.app.reverter_rejeicao(self),
                ).props("color=primary outline")
            ui.space()
            self.label_contagem = ui.label().classes("text-grey-7 q-mr-md")
            # Export da visão atual (respeita filtro + fase + ordenação corrente).
            with ui.button("Exportar", icon="download").props("color=secondary outline"):
                with ui.menu():
                    ui.menu_item("Excel (.xlsx)",
                                 on_click=lambda: self.app.exportar(self, "xlsx"))
                    ui.menu_item("CSV (.csv)",
                                 on_click=lambda: self.app.exportar(self, "csv"))

        dados = self._carregar_dados()
        self.grid = ui.aggrid({
            "columnDefs": _colunas(com_motivo=(self.fase == "rejeitada")),
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

    def _where_params(self) -> tuple[Optional[str], list]:
        """Combina a cláusula da fase com o filtro global da App."""
        fase_where = None if self.fase == "geral" else db.where_por_fase(self.fase)
        filtro_where, params = self.app.filtro_where_params()
        where = db._combinar_where(fase_where, filtro_where)
        return where, params

    def _carregar_dados(self) -> list[dict]:
        where, params = self._where_params()
        rows = db.listar_amostras(self.app.con, where=where, params=params)
        return [_linha_para_dict(r) for r in rows]

    def recarregar(self) -> None:
        dados = self._carregar_dados()
        self.grid.options["rowData"] = dados
        self.grid.update()
        self.label_contagem.text = f"{len(dados)} amostra(s)"


class App:
    def __init__(self):
        self.con = db.conectar()   # schema já foi criado no on_startup
        self.tabs: dict[str, FaseTab] = {}
        self._cards: dict[str, ui.label] = {}
        # Estado dos filtros globais (compartilhado por todas as abas).
        self.f_ano: Optional[int] = None
        self.f_municipio: Optional[str] = None
        self.f_busca_ni: str = ""
        self.f_flags: list[str] = []        # flags específicas (qualquer uma)
        self.f_com_flags: Optional[bool] = None  # True/False/None

    def filtro_where_params(self) -> tuple[Optional[str], list]:
        """(where, params) do filtro global corrente (sem a cláusula de fase)."""
        return db.construir_filtro(
            ano=self.f_ano,
            municipio=self.f_municipio,
            busca_ni=self.f_busca_ni or None,
            flags_qualquer=self.f_flags or None,
            com_flags=self.f_com_flags,
        )

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

    async def abrir_dialogo_rejeicao(self, tab: FaseTab) -> None:
        """Abre diálogo para escolher o motivo e rejeitar a seleção (só pendentes)."""
        chaves = await self._chaves_selecionadas(tab)
        if not chaves:
            ui.notify("Selecione ao menos uma amostra.", type="warning")
            return

        with ui.dialog() as dialogo, ui.card():
            ui.label(f"Rejeitar {len(chaves)} amostra(s)").classes("text-bold")
            ui.label("Escolha o motivo da rejeição:").classes("text-grey-7")
            motivo_sel = ui.select(
                list(db.MOTIVOS_REJEICAO), label="Motivo",
                value=db.MOTIVOS_REJEICAO[0],
            ).props("dense").classes("w-64")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancelar", on_click=dialogo.close).props("flat")
                ui.button(
                    "Confirmar rejeição", icon="block",
                    on_click=lambda: self._confirmar_rejeicao(
                        dialogo, chaves, motivo_sel.value
                    ),
                ).props("color=negative")
        dialogo.open()

    def _confirmar_rejeicao(self, dialogo, chaves: list[str], motivo: str) -> None:
        if not motivo:
            ui.notify("Selecione um motivo.", type="warning")
            return
        try:
            n = db.rejeitar(self.con, chaves, motivo)
        except db.TransicaoInvalida as e:
            ui.notify(str(e), type="negative")
            return
        except ValueError as e:
            ui.notify(str(e), type="negative")
            return
        dialogo.close()
        ui.notify(f"{n} amostra(s) rejeitada(s) — {motivo}.", type="positive")
        self.refresh()

    async def reverter_rejeicao(self, tab: FaseTab) -> None:
        chaves = await self._chaves_selecionadas(tab)
        if not chaves:
            ui.notify("Selecione ao menos uma amostra.", type="warning")
            return
        n = db.reverter_rejeicao(self.con, chaves)
        ui.notify(f"{n} amostra(s) devolvida(s) a Pendente.", type="positive")
        self.refresh()

    def _filtrar_nao_coletadas(self, chaves: list[str]) -> tuple[list[str], int]:
        """Elegíveis para coletar = pendentes (não coletadas E não rejeitadas).

        Amostras já no fluxo OU rejeitadas não reentram (decisão do usuário).
        """
        ph = db._placeholders(len(chaves))
        ja = {
            row["chave"]
            for row in self.con.execute(
                f"SELECT chave FROM amostras WHERE chave IN ({ph}) "
                f"AND (coletada = 1 OR rejeitada = 1)",
                chaves,
            ).fetchall()
        }
        elegiveis = [c for c in chaves if c not in ja]
        return elegiveis, len(ja)

    # -- export ------------------------------------------------------------ #
    def exportar(self, tab: FaseTab, formato: str) -> None:
        """Exporta a visão da aba (filtro + fase + ordenação) em xlsx/csv."""
        where, params = tab._where_params()
        rows = db.listar_amostras(self.con, where=where, params=params)
        if not rows:
            ui.notify("Nada para exportar na visão atual.", type="warning")
            return

        nome_aba = "geral" if tab.fase == "geral" else tab.fase
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        if formato == "xlsx":
            conteudo = export.para_xlsx_bytes(rows, sheet_name=nome_aba)
            fname = f"reprocesso_{nome_aba}_{ts}.xlsx"
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            conteudo = export.para_csv_bytes(rows)
            fname = f"reprocesso_{nome_aba}_{ts}.csv"
            media = "text/csv"
        ui.download(conteudo, fname, media)
        ui.notify(f"Exportando {len(rows)} amostra(s) — {fname}", type="positive")

    # -- render ------------------------------------------------------------ #
    def refresh(self) -> None:
        for tab in self.tabs.values():
            tab.recarregar()
        # Métricas refletem o subconjunto sob os filtros correntes (Fase 4).
        where, params = self.filtro_where_params()
        cont = db.contagens_por_fase(self.con, where=where, params=params)
        total = cont["total"]
        self._cards["total"].text = str(total)
        for chave in ("coletada", "extraida", "pcr_feito", "rejeitada"):
            self._cards[chave].text = str(cont[chave])
            pct = (cont[chave] / total * 100) if total else 0
            self._cards[f"{chave}_pct"].text = f"{pct:.0f}% do total"

    def aplicar_filtros(self) -> None:
        """Lê os controles, atualiza o estado e recarrega tudo."""
        self.f_ano = self._ctl_ano.value or None
        self.f_municipio = self._ctl_municipio.value or None
        self.f_busca_ni = (self._ctl_busca.value or "").strip()
        flags = list(self._ctl_flags.value or [])
        self.f_flags = flags
        self.refresh()

    def limpar_filtros(self) -> None:
        self._ctl_ano.value = None
        self._ctl_municipio.value = None
        self._ctl_busca.value = ""
        self._ctl_flags.value = []
        self.aplicar_filtros()

    def _montar_filtros(self) -> None:
        """Painel de filtros globais (Fase 4): ano, município, busca NI, flags."""
        anos = db.valores_distintos(self.con, "ano_verdade")
        municipios = db.valores_distintos(self.con, "municipio")
        # Flags disponíveis nos dados (para o multi-select).
        flags_disp = sorted({
            t for r in self.con.execute(
                "SELECT DISTINCT flags FROM amostras WHERE flags != ''"
            ).fetchall()
            for t in r["flags"].split(";") if t
        })

        with ui.card().classes("w-full q-mb-md"):
            with ui.row().classes("w-full items-end gap-3"):
                self._ctl_busca = ui.input(
                    "Buscar NI", placeholder="ex.: D1264"
                ).props("clearable dense").classes("w-40").on(
                    "keydown.enter", lambda: self.aplicar_filtros()
                )
                self._ctl_ano = ui.select(
                    {a: str(a) for a in anos}, label="Ano", clearable=True
                ).props("dense").classes("w-32")
                self._ctl_municipio = ui.select(
                    municipios, label="Município", clearable=True, with_input=True
                ).props("dense").classes("w-64")
                self._ctl_flags = ui.select(
                    flags_disp, label="Flags", multiple=True, clearable=True
                ).props("dense use-chips").classes("w-72")

                ui.button("Filtrar", icon="filter_alt",
                          on_click=lambda: self.aplicar_filtros()).props("color=primary")
                ui.button("Limpar", icon="clear",
                          on_click=lambda: self.limpar_filtros()).props("flat")

    def _card(self, titulo: str, chave: str, cor: str, com_pct: bool = False) -> None:
        with ui.card().classes("items-center").style(f"border-top: 4px solid {cor}"):
            self._cards[chave] = ui.label("0").classes("text-2xl text-bold")
            ui.label(titulo).classes("text-grey-7 text-sm")
            if com_pct:
                self._cards[f"{chave}_pct"] = ui.label("").classes("text-grey-6 text-xs")

    def construir(self, logout_callback=None) -> None:
        with ui.row().classes("items-baseline gap-2 q-mb-sm w-full justify-between"):
            with ui.row().classes("items-baseline gap-2"):
                ui.label("Reprocesso Dengue — LACEN-RS").classes("text-h5")
                ui.label("controle de coleta · extração · PCR").classes("text-grey-6 text-sm")
            if logout_callback:
                ui.button("Sair", icon="logout", on_click=logout_callback).props(
                    "flat size=sm color=grey-7"
                )

        with ui.row().classes("gap-4 q-mb-md"):
            self._card("Total", "total", "#607d8b")
            self._card("Coletadas", "coletada", "#2196f3", com_pct=True)
            self._card("Extraídas", "extraida", "#ff9800", com_pct=True)
            self._card("PCR feito", "pcr_feito", "#4caf50", com_pct=True)
            self._card("Rejeitadas", "rejeitada", "#e53935", com_pct=True)

        self._montar_filtros()

        with ui.tabs().classes("w-full") as tabs:
            t_geral = ui.tab("Geral")
            t_col = ui.tab("Coletadas")
            t_ext = ui.tab("Extraídas")
            t_pcr = ui.tab("PCR feito")
            t_rej = ui.tab("Rejeitadas")

        with ui.tab_panels(tabs, value=t_geral).classes("w-full"):
            with ui.tab_panel(t_geral):
                self.tabs["geral"] = FaseTab(self, "geral")
            with ui.tab_panel(t_col):
                self.tabs["coletada"] = FaseTab(self, "coletada")
            with ui.tab_panel(t_ext):
                self.tabs["extraida"] = FaseTab(self, "extraida")
            with ui.tab_panel(t_pcr):
                self.tabs["pcr_feito"] = FaseTab(self, "pcr_feito")
            with ui.tab_panel(t_rej):
                self.tabs["rejeitada"] = FaseTab(self, "rejeitada")

        self.refresh()
        if db.contar(self.con) == 0:
            ui.notify(
                "Banco vazio — rode 'python -m src.importer' para popular.",
                type="warning",
                timeout=0,
            )


def main() -> None:
    auth.build_login_page()

    @ui.page("/")
    def index():
        if not auth.is_authenticated():
            ui.navigate.to("/login")
            return
        App().construir(
            logout_callback=auth.logout if auth.AUTH_ENABLED else None
        )

    _port = int(os.environ.get("PORT", "8080"))
    _host = os.environ.get("HOST", "127.0.0.1")
    _secret = os.environ.get("APP_SECRET", "dev-secret-local-only")
    ui.run(
        title="Reprocesso Dengue",
        reload=False,
        port=_port,
        host=_host,
        storage_secret=_secret,
    )


# NiceGUI executa o módulo; o guard padrão do framework é __mp_main__.
if __name__ in {"__main__", "__mp_main__"}:
    main()
