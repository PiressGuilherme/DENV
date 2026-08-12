"""UI NiceGUI — tracker de reprocesso de dengue, organizado por fases (kanban).

Fluxo de trabalho por abas (ver Seção 4 da ESPECIFICACAO.md). Abas:

    - Geral: TODAS as amostras, na ordenação canônica (Seção 3.3), com badge da
      fase atual. Visão de auditoria/busca. Daqui marca-se "Coletada" em lote;
      amostras que já entraram no fluxo (qualquer status) NÃO podem reentrar.
    - Coletadas / Extraídas / PCR feito / Sequenciadas: recortes por fase. Cada
      aba avança a etapa seguinte em lote, com AVANÇO ESTRITO (bloqueia fora de
      ordem), e permite RETROCEDER (desmarcar).

Sequenciadas é um SUBCONJUNTO de PCR feito, não um sucessor: a amostra enviada
para sequenciamento continua listada em PCR feito, que é onde os resultados de
PCR (Ct por sorotipo) são consultados. Por isso os cards não somam o total.

A aba PCR feito tem ainda o import de resultados (xlsx/csv com NI + DEN1..DEN4),
com prévia antes de gravar — ver abrir_dialogo_resultados e src/resultados.py.

Toda ação persiste no PostgreSQL, grava evento de auditoria e atualiza as grades +
contadores. A lógica de fase mora em db.py; aqui é só a casca de UI — as abas,
cards, badges e rótulos são DERIVADOS de db.ETAPAS_DEF.

Uso:
    python -m src.app
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Optional

from nicegui import app as _nicegui_app
from nicegui import ui

from src import auth, db, export, resultados, parser_termociclador


def _startup_db() -> None:
    """Cria schema e popula o banco no primeiro boot (roda em thread — não bloqueia porta)."""
    import traceback

    if not db._DATABASE_URL:
        print("[startup] DATABASE_URL não definido — pulando setup do banco.", flush=True)
        return

    # 1. Schema (idempotente)
    try:
        con = db.init_db()
        n = db.contar(con)
        con.close()
        print(f"[startup] Schema OK. Amostras no banco: {n}", flush=True)
    except Exception:
        print("[startup] FALHA ao criar schema / conectar:", flush=True)
        traceback.print_exc()
        return

    # 2. Import (só se vazio)
    if n == 0:
        print("[startup] Banco vazio — importando xlsx...", flush=True)
        try:
            from src.importer import importar
            r = importar(verificar_sanidade=False)
            print(f"[startup] Import concluído: {r.amostras_unicas} amostras "
                  f"({r.inseridas} inseridas).", flush=True)
        except Exception:
            print("[startup] FALHA no import:", flush=True)
            traceback.print_exc()


_nicegui_app.on_startup(
    lambda: threading.Thread(target=_startup_db, daemon=True).start()
)

_FASE_GERAL = "geral"

# Abas, na ordem de exibição: Geral + uma por etapa + Rejeitadas. Derivado de
# db.ETAPAS_DEF — uma etapa nova ganha aba, card e badge sem editar app.py.
_ABAS: tuple[tuple[str, str], ...] = (
    (_FASE_GERAL, "Geral"),
    *((e.chave, e.label_aba) for e in db.ETAPAS_DEF),
    (db.FASE_REJEITADA, "Rejeitadas"),
)

# Nome da aba (ui.tab) -> chave de fase. Usado no on_change para o load lazy.
_TAB_FASE = {rotulo: fase for fase, rotulo in _ABAS}

# Fases com card de métrica (todas menos a Geral, que já é o card "Total").
# ATENÇÃO: os cards NÃO somam o total — 'Sequenciadas' é um subconjunto de
# 'PCR feito' (db.Etapa.exclusiva=False), então essas amostras contam nos dois.
_FASES_COM_CARD: tuple[str, ...] = tuple(
    fase for fase, _ in _ABAS if fase != _FASE_GERAL
)

# Etapa "alvo" de cada aba (o botão de avanço marca esta etapa).
# Geral/Pendente marcam a 1ª etapa; cada aba de etapa marca a SEGUINTE.
_PROXIMA_ETAPA = {
    _FASE_GERAL: db.ETAPAS[0],
    db.FASE_PENDENTE: db.ETAPAS[0],
    **{
        etapa.chave: db.ETAPAS[i + 1]
        for i, etapa in enumerate(db.ETAPAS_DEF)
        if i + 1 < len(db.ETAPAS_DEF)
    },
}


def _badge_html(fase: str) -> str:
    """HTML do badge da fase (renderizado via html_columns do NiceGUI)."""
    return (
        f'<span style="padding:2px 8px;border-radius:10px;color:white;'
        f'font-size:11px;background:{db.COR_FASE[fase]}">{db.LABEL_FASE[fase]}</span>'
    )


def _ct_para_display(valor) -> str:
    """Ct para exibição: 
    - None (não testado) -> vazio
    - -1.0 (não detectado) -> 'ND' (Not Detected)
    - >0 -> valor formatado com 1 casa decimal
    """
    if valor is None:
        return ""
    if valor == -1.0:
        return "ND"
    return f"{float(valor):.1f}"


def _linha_para_dict(r) -> dict:
    """Converte uma linha do banco no dict que o AG-Grid consome."""
    return {
        "chave": r["chave"],                    # usada como rowId
        "ni": r["ni_original"] or r["chave"],   # display
        "numero": r["numero_sequencial"],       # numérico p/ ordenação nativa
        "ano": r["ano_verdade"],
        "municipio": r["municipio"] or "",
        "data_coleta": r["data_coleta"] or "",
        "data_sintomas": r["data_sintomas"] or "",
        "caso": r["caso"] or "",
        "fase": _badge_html(db.fase_da_linha(r)),   # HTML (ver html_columns)
        "motivo": r["motivo_rejeicao"] or "",
        "sorotipo": resultados.sorotipo_de(r),
        **{
            db.coluna_ct(s): _ct_para_display(r[db.coluna_ct(s)])
            for s in db.SOROTIPOS
        },
        **{
            campo: _ct_para_display(r[campo])
            for campo in db.COLUNAS_CI
        },
        "flags": r["flags"] or "",
        "n_origem": r["n_origem"],
    }


def _colunas(*, com_motivo: bool = False, com_resultado: bool = False) -> tuple[list[dict], int]:
    """Colunas da grade + índice da coluna 'Fase' (que renderiza HTML).

    Devolve o índice em vez de mantê-lo como constante: com colunas condicionais
    (motivo, resultado) um índice fixo quebraria silenciosamente — a coluna Fase
    passaria a exibir HTML cru se algo entrasse antes dela.
    """
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
        {"headerName": "Fase", "field": "fase", "width": 130},
    ]
    idx_fase = len(cols) - 1
    if com_motivo:
        cols.append({"headerName": "Motivo", "field": "motivo", "filter": True,
                     "width": 180})
    if com_resultado:
        cols.append({"headerName": "Sorotipo", "field": "sorotipo", "filter": True,
                     "width": 130})
        cols += [
            {"headerName": f"{s} (Ct)", "field": db.coluna_ct(s),
             "type": "numericColumn", "width": 100}
            for s in db.SOROTIPOS
        ]
        # Controle Interno (duas colunas, uma por arquivo)
        cols += [
            {"headerName": f"{cab}", "field": campo,
             "type": "numericColumn", "width": 100}
            for campo, cab in (
                (db.COLUNAS_CI[0], "CI 1-4 (Ct)"),
                (db.COLUNAS_CI[1], "CI 2-3 (Ct)"),
            )
        ]
    cols += [
        {"headerName": "Flags", "field": "flags", "filter": True, "width": 260},
        {"headerName": "Nº origem", "field": "n_origem", "type": "numericColumn",
         "width": 110},
    ]
    return cols, idx_fase


class FaseTab:
    """Uma aba: grade + barra de ações de avanço/retrocesso para uma fase."""

    def __init__(self, app: "App", fase: str):
        self.app = app
        self.fase = fase  # _FASE_GERAL, uma chave de db.ETAPAS ou db.FASE_REJEITADA
        self.grid: Optional[ui.aggrid] = None
        self._carregado = False  # lazy: só consulta o banco quando a aba é vista
        self._montar()

    def _montar(self) -> None:
        etapa = _PROXIMA_ETAPA.get(self.fase)
        with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
            if etapa:
                ui.button(
                    f"Marcar {db.ETAPA_POR_CHAVE[etapa].label}",
                    icon="arrow_forward",
                    on_click=lambda: self.app.avancar(self, etapa),
                ).props("color=primary")
            # Rejeitar: só na Geral (rejeita amostras pendentes).
            if self.fase == _FASE_GERAL:
                ui.button(
                    "Rejeitar",
                    icon="block",
                    on_click=lambda: self.app.abrir_dialogo_rejeicao(self),
                ).props("color=negative outline")
            # Retroceder: só nas abas de etapa concreta (não na Geral nem Pendente).
            if self.fase in db.ETAPA_POR_CHAVE:
                ui.button(
                    f"Desmarcar {db.LABEL_FASE[self.fase]}",
                    icon="undo",
                    on_click=lambda: self.app.retroceder(self, self.fase),
                ).props("color=negative outline")
            # Import de resultados: só na etapa que produz PCR.
            if self.fase == db.ETAPA_RESULTADO:
                ui.button(
                    "Importar resultados",
                    icon="upload_file",
                    on_click=self.app.abrir_dialogo_resultados,
                ).props("color=secondary outline")
                ui.button(
                    "Importar do Termociclador",
                    icon="biotech",
                    on_click=self.app.abrir_dialogo_termociclador,
                ).props("color=primary outline")
            # Reverter: só na aba Rejeitadas (volta a Pendente).
            if self.fase == db.FASE_REJEITADA:
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

        # Grade criada VAZIA: os dados entram via garantir_carregado() só quando
        # a aba é vista pela 1ª vez (lazy). Evita carregar todas as abas no load.
        colunas, idx_fase = _colunas(
            com_motivo=(self.fase == db.FASE_REJEITADA),
            com_resultado=(self.fase in db.FASES_COM_RESULTADO),
        )
        self.grid = ui.aggrid({
            "columnDefs": colunas,
            # API de seleção do AG-Grid v33+ (checkboxSelection no colDef foi removido):
            "rowSelection": {
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckbox": True,
                "enableClickSelection": False,
            },
            "defaultColDef": {"sortable": True, "resizable": True},
            ":getRowId": "params => params.data.chave",
            "rowData": [],
        }, html_columns=[idx_fase], auto_size_columns=False).classes(
            "w-full"
        ).style("height: 65vh")

    def garantir_carregado(self) -> None:
        """Carrega os dados da aba se ainda não foram carregados (lazy)."""
        if not self._carregado:
            self.recarregar()

    def invalidar(self) -> None:
        """Marca a aba como desatualizada (recarrega na próxima vez que for vista)."""
        self._carregado = False

    def _where_params(self) -> tuple[Optional[str], list]:
        """Combina a cláusula da fase com o filtro global da App."""
        fase_where = None if self.fase == _FASE_GERAL else db.where_por_fase(self.fase)
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
        self._carregado = True


class App:
    def __init__(self):
        self.con = db.conectar()   # schema já foi criado no on_startup
        self.tabs: dict[str, FaseTab] = {}
        self._fase_ativa = _FASE_GERAL  # aba visível (para o refresh lazy)
        self._cards: dict[str, ui.label] = {}
        # Estado dos filtros globais (compartilhado por todas as abas).
        self.f_ano: Optional[int] = None
        self.f_municipio: Optional[str] = None
        self.f_busca_ni: str = ""
        self.f_flags: list[str] = []        # flags específicas (qualquer uma)
        self.f_com_flags: Optional[bool] = None  # True/False/None
        self.f_sorotipo: Optional[str] = None    # 'DEN1'..'DEN4' ou não detectado

    def close(self) -> None:
        """Fecha a conexão (chamado quando o client NiceGUI é descartado)."""
        try:
            self.con.close()
        except Exception:
            pass

    def filtro_where_params(self) -> tuple[Optional[str], list]:
        """(where, params) do filtro global corrente (sem a cláusula de fase)."""
        return db.construir_filtro(
            ano=self.f_ano,
            municipio=self.f_municipio,
            busca_ni=self.f_busca_ni or None,
            flags_qualquer=self.f_flags or None,
            com_flags=self.f_com_flags,
            sorotipo=self.f_sorotipo,
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
        if tab.fase == _FASE_GERAL and etapa == db.ETAPAS[0]:
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
        ui.notify(f"{n} amostra(s) → {db.LABEL_FASE[etapa]}.", type="positive")
        self.refresh()

    async def retroceder(self, tab: FaseTab, etapa: str) -> None:
        chaves = await self._chaves_selecionadas(tab)
        if not chaves:
            ui.notify("Selecione ao menos uma amostra.", type="warning")
            return
        n = db.retroceder_fase(self.con, chaves, etapa)
        ui.notify(f"{n} amostra(s) retrocedida(s) de {db.LABEL_FASE[etapa]}.", type="positive")
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

    # -- import de resultados ---------------------------------------------- #
    def abrir_dialogo_resultados(self) -> None:
        """Diálogo de import de resultados de PCR (xlsx/csv com NI + DEN1..DEN4).

        Fluxo em DOIS passos — prévia e confirmação. O usuário vê exatamente o
        que será gravado e o que foi descartado (com o motivo) antes de qualquer
        escrita; sem isso, um cabeçalho errado viraria "0 amostras atualizadas"
        sem explicação.
        """
        with ui.dialog() as dialogo, ui.card().classes("w-[46rem]"):
            # Botão de fechar SEMPRE presente, fora das áreas que são limpas a
            # cada upload — é o que garante que o modal nunca prenda o usuário.
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Importar resultados de PCR").classes("text-bold text-lg")
                ui.space()
                ui.button(icon="close", on_click=dialogo.close).props("flat round dense")
            ui.label(
                f"Planilha .xlsx ou .csv com as colunas NI, {', '.join(db.SOROTIPOS)}. "
                "Célula vazia = sorotipo não detectado; preenchida = valor de Ct."
            ).classes("text-grey-7 text-sm")

            area_resultado = ui.column().classes("w-full")
            rodape = ui.row().classes("w-full justify-end gap-2")

            async def ao_subir(evento) -> None:
                area_resultado.clear()
                self._rodape_fechar(rodape, dialogo)
                try:
                    # NiceGUI 3.x: o arquivo vem em evento.file e read() é async.
                    conteudo = await evento.file.read()
                    df = resultados.ler_arquivo(conteudo, evento.file.name)
                    plano = resultados.montar_plano(
                        df, db.indice_para_resultados(self.con)
                    )
                except resultados.ArquivoInvalido as e:
                    ui.notify(str(e), type="negative", timeout=8000)
                    return
                except Exception as e:  # noqa: BLE001
                    # Qualquer falha inesperada tem que virar aviso visível. Sem
                    # isso a exceção só apareceria no log e o modal ficaria
                    # parado, sem prévia e sem explicação.
                    ui.notify(f"Falha ao ler a planilha: {e}", type="negative",
                              timeout=10000)
                    return

                self._render_previa(area_resultado, plano)
                self._render_acoes(rodape, dialogo, plano)

            ui.upload(
                on_upload=ao_subir, max_files=1, auto_upload=True,
                label="Selecione a planilha",
            ).props('accept=".xlsx,.xlsm,.csv"').classes("w-full")

            self._rodape_fechar(rodape, dialogo)
        dialogo.open()

    # -- import de resultados do termociclador ----------------------------- #
    def abrir_dialogo_termociclador(self) -> None:
        """Diálogo de import de resultados do termociclador (2 arquivos: 1-4 e 2-3)."""
        # Estado local do diálogo
        estado = {
            "arquivo_1_4": None,      # (nome, bytes)
            "arquivo_2_3": None,      # (nome, bytes)
            "resultado_parse": None,  # ResultadoParseTermociclador merged
            "ano_definido": None,     # ano informado pelo usuário
            "conflitos": None,        # lista de ConflitoCampo
            "resolvidas": None,       # dict {chave: cts}
            "nao_encontradas": None,  # lista
            "ano_ambiguo": None,      # lista
        }

        with ui.dialog() as dialogo, ui.card().classes("w-[52rem]"):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Importar do Termociclador").classes("text-bold text-lg")
                ui.space()
                ui.button(icon="close", on_click=dialogo.close).props("flat round dense")
            
            ui.label(
                "Selecione os dois arquivos de corrida: "
                "<b>Dengue 1-4</b> (FAM=DEN4, VIC=DEN1, Cy5=CI) e "
                "<b>Dengue 2-3</b> (FAM=DEN2, VIC=DEN3, Cy5=CI). "
                "Amostras com Sample Type ≠ 'Unknown' (controles CN/CP) são ignoradas."
            ).classes("text-grey-7 text-sm")

            # Área de upload dos dois arquivos
            with ui.row().classes("w-full gap-4 q-mt-md"):
                # Arquivo 1-4
                with ui.column().classes("w-1/2"):
                    ui.label("Dengue 1-4").classes("text-bold")
                    self._upload_arquivo_termociclador(
                        estado, "arquivo_1_4", "1-4",
                        lambda: self._processar_termociclador(estado, dialogo, area_previa, rodape)
                    )
                
                # Arquivo 2-3
                with ui.column().classes("w-1/2"):
                    ui.label("Dengue 2-3").classes("text-bold")
                    self._upload_arquivo_termociclador(
                        estado, "arquivo_2_3", "2-3",
                        lambda: self._processar_termociclador(estado, dialogo, area_previa, rodape)
                    )

            # Área de prévia (preenchida após processar)
            area_previa = ui.column().classes("w-full q-mt-md")
            
            # Rodapé com ações
            rodape = ui.row().classes("w-full justify-end gap-2 q-mt-md")
            self._rodape_fechar(rodape, dialogo)

        dialogo.open()

    def _upload_arquivo_termociclador(self, estado: dict, chave_estado: str, label: str, callback) -> None:
        """Cria widget de upload para um arquivo do termociclador."""
        async def ao_subir(evento):
            conteudo = await evento.file.read()
            estado[chave_estado] = (evento.file.name, conteudo)
            ui.notify(f"{label}: {evento.file.name} carregado", type="positive")
            # Se já temos os dois arquivos, processa automaticamente
            if estado["arquivo_1_4"] and estado["arquivo_2_3"]:
                callback()
        
        ui.upload(
            on_upload=ao_subir,
            max_files=1,
            auto_upload=True,
            label=f"Selecione Dengue {label}",
        ).props('accept=".xlsx,.xlsm"').classes("w-full")

    def _processar_termociclador(self, estado: dict, dialogo, area_previa, rodape) -> None:
        """Processa os dois arquivos e mostra prévia ou pede ano."""
        area_previa.clear()
        rodape.clear()
        self._rodape_fechar(rodape, dialogo)

        try:
            # Parse arquivo 1-4
            nome_1_4, conteudo_1_4 = estado["arquivo_1_4"]
            r1 = parser_termociclador.parse_arquivo_termociclador(conteudo_1_4, nome_1_4)
            
            # Parse arquivo 2-3
            nome_2_3, conteudo_2_3 = estado["arquivo_2_3"]
            r2 = parser_termociclador.parse_arquivo_termociclador(conteudo_2_3, nome_2_3)
            
            # Merge
            merged = parser_termociclador.merge_arquivos_termociclador(r1, r2)
            
            if merged.erros:
                ui.notify("; ".join(merged.erros), type="negative", timeout=10000)
                return
            
            estado["resultado_parse"] = merged
            
            # Se há sample IDs sem ano, pede o ano
            if merged.sample_ids_sem_ano:
                self._pedir_ano_termociclador(estado, dialogo, area_previa, rodape, merged.sample_ids_sem_ano)
            else:
                # Resolve direto no banco
                self._resolver_e_mostrar_previa(estado, dialogo, area_previa, rodape, None)
        
        except Exception as e:
            ui.notify(f"Erro ao processar arquivos: {e}", type="negative", timeout=10000)

    def _pedir_ano_termociclador(self, estado: dict, dialogo, area_previa, rodape, sample_ids_sem_ano: list[str]) -> None:
        """Mostra diálogo para informar o ano das amostras sem ano no Sample ID."""
        with area_previa:
            ui.label(f"⚠️ {len(sample_ids_sem_ano)} amostra(s) não têm ano no Sample ID:").classes("text-orange-8 text-bold")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for sid in sample_ids_sem_ano[:20]:
                    ui.chip(sid).classes("bg-grey-2")
                if len(sample_ids_sem_ano) > 20:
                    ui.label(f"... e mais {len(sample_ids_sem_ano) - 20}").classes("text-grey-6")
            
            ui.separator().classes("q-my-md")
            ui.label("Informe o ano de coleta para estas amostras:").classes("text-grey-7")
            
            ano_input = ui.number("Ano", value=2025, min=2020, max=2030).props("dense").classes("w-32")
            
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancelar", on_click=dialogo.close).props("flat")
                ui.button(
                    "Continuar", icon="arrow_forward",
                    on_click=lambda: self._resolver_e_mostrar_previa(
                        estado, dialogo, area_previa, rodape, ano_input.value
                    ),
                ).props("color=primary")
        
        self._rodape_fechar(rodape, dialogo)

    def _resolver_e_mostrar_previa(self, estado: dict, dialogo, area_previa, rodape, ano_padrao: Optional[int]) -> None:
        """Resolve amostras no banco e mostra prévia com conflitos."""
        area_previa.clear()
        rodape.clear()
        self._rodape_fechar(rodape, dialogo)

        try:
            # Prepara dados para gravação
            dados_gravacao = parser_termociclador.preparar_para_gravacao(
                estado["resultado_parse"], ano_padrao
            )
            
            # Resolve no banco
            resolvidas, nao_encontradas, ano_ambiguo = db.resolver_amostras_termociclador(
                self.con, dados_gravacao
            )
            
            estado["resolvidas"] = resolvidas
            estado["nao_encontradas"] = nao_encontradas
            estado["ano_ambiguo"] = ano_ambiguo
            
            # Mostra prévia
            self._render_previa_termociclador(estado, dialogo, area_previa, rodape)
        
        except Exception as e:
            ui.notify(f"Erro ao resolver amostras: {e}", type="negative", timeout=10000)

    def _render_previa_termociclador(self, estado: dict, dialogo, area_previa, rodape) -> None:
        """Renderiza a prévia do import do termociclador com conflitos."""
        resolvidas = estado["resolvidas"]
        nao_encontradas = estado["nao_encontradas"]
        ano_ambiguo = estado["ano_ambiguo"]
        
        with area_previa:
            # Resumo
            with ui.row().classes("items-center gap-4 q-mt-sm"):
                ui.label(f"✅ {len(resolvidas)} amostra(s) encontradas no banco").classes("text-positive text-bold")
                if nao_encontradas:
                    ui.label(f"❌ {len(nao_encontradas)} não encontrada(s)").classes("text-negative")
                if ano_ambiguo:
                    ui.label(f"⚠️ {len(ano_ambiguo)} com ano ambíguo").classes("text-orange-8")
            
            if not resolvidas:
                ui.label("Nenhuma amostra para gravar.").classes("text-grey-6")
                self._rodape_fechar(rodape, dialogo)
                return
            
            # Busca valores atuais no banco para detectar conflitos
            chaves = list(resolvidas.keys())
            ph = db._placeholders(len(chaves))
            todos_campos = (*db.COLUNAS_CT, *db.COLUNAS_CI)
            rows_atuais = self.con.execute(
                f"SELECT chave, {', '.join(todos_campos)} FROM amostras WHERE chave IN ({ph})",
                chaves,
            ).fetchall()
            
            atuais_por_chave = {r["chave"]: r for r in rows_atuais}
            
            # Detecta conflitos
            conflitos = []
            for chave, cts_novos in resolvidas.items():
                atuais = atuais_por_chave.get(chave, {})
                for campo in todos_campos:
                    valor_novo = cts_novos.get(campo)
                    valor_atual = atuais.get(campo) if atuais else None
                    
                    if (valor_novo is not None and valor_atual is not None
                            and not db.mesmo_ct(valor_atual, valor_novo)):
                        conflitos.append({
                            "chave": chave,
                            "campo": campo,
                            "valor_atual": valor_atual,
                            "valor_novo": valor_novo,
                        })
            
            estado["conflitos"] = conflitos
            
            # Mostra conflitos com checkboxes
            if conflitos:
                ui.label(f"⚠️ {len(conflitos)} conflito(s) de valor detectado(s):").classes("text-orange-8 text-bold q-mt-md")
                ui.label("Marque os campos que deseja sobrescrever:").classes("text-grey-7")
                
                checkboxes = {}  # (chave, campo) -> ui.checkbox
                
                with ui.column().classes("w-full q-mt-sm"):
                    for i, conf in enumerate(conflitos):
                        with ui.row().classes("items-center gap-2"):
                            cb = ui.checkbox(
                                f"{conf['chave']} · {conf['campo'].upper()}: "
                                f"atual={conf['valor_atual']:.1f} → novo={conf['valor_novo']:.1f}"
                            ).props("dense")
                            checkboxes[(conf['chave'], conf['campo'])] = cb
                
                estado["checkboxes_conflitos"] = checkboxes
            
            # Ações
            with rodape:
                if conflitos:
                    ui.button(
                        "Sobrescrever marcados e gravar", icon="save",
                        on_click=lambda: self._confirmar_gravacao_termociclador(
                            estado, dialogo, checkboxes
                        ),
                    ).props("color=primary")
                    ui.button("Gravar apenas sem conflitos", icon="save",
                        on_click=lambda: self._confirmar_gravacao_termociclador(
                            estado, dialogo, {}
                        ),
                    ).props("color=secondary outline")
                else:
                    ui.button(
                        f"Gravar {len(resolvidas)} amostra(s)", icon="save",
                        on_click=lambda: self._confirmar_gravacao_termociclador(
                            estado, dialogo, {}
                        ),
                    ).props("color=primary")
                
                ui.button("Cancelar", on_click=dialogo.close).props("flat")

    def _confirmar_gravacao_termociclador(self, estado: dict, dialogo, checkboxes: dict) -> None:
        """Confirma a gravação dos resultados do termociclador."""
        # Coleta quais conflitos o usuário marcou para sobrescrever
        sobrescrever = set()
        for (chave, campo), cb in checkboxes.items():
            if cb.value:
                sobrescrever.add((chave, campo))
        
        try:
            resultado = db.gravar_resultados_termociclador(
                self.con,
                estado["resolvidas"],
                sobrescrever=sobrescrever if sobrescrever else None,
            )
            
            dialogo.close()
            
            # Monta mensagem de resultado
            msgs = []
            # O emoji acompanha o resultado: um ✅ fixo faria "0 atualizadas" ser
            # lido como sucesso, que foi exatamente o que confundiu no campo.
            icone = "✅" if resultado.gravados > 0 else "⚠️"
            msgs.append(f"{icone} {resultado.gravados} amostra(s) atualizada(s) ({resultado.campos_gravados} campo(s))")
            if resultado.conflitos:
                msgs.append(f"⚠️ {len(resultado.conflitos)} conflito(s) não autorizado(s) — não gravados")
            if resultado.nao_encontradas:
                msgs.append(f"❌ {len(resultado.nao_encontradas)} não encontrada(s) no banco")
            if resultado.ano_ambiguo:
                msgs.append(f"⚠️ {len(resultado.ano_ambiguo)} com ano ambíguo")
            
            ui.notify(" | ".join(msgs), type="positive" if resultado.gravados > 0 else "warning", timeout=10000)
            self.refresh()
        
        except Exception as e:
            ui.notify(f"Falha ao gravar: {e}", type="negative", timeout=10000)

    def _rodape_fechar(self, rodape, dialogo) -> None:
        rodape.clear()
        with rodape:
            ui.button("Fechar", on_click=dialogo.close).props("flat")

    def _render_previa(self, area, plano) -> None:
        """Resumo do que será gravado + ignoradas agrupadas por motivo."""
        with area:
            with ui.row().classes("items-center gap-4 q-mt-sm"):
                ui.label(f"{len(plano.aplicaveis)} serão atualizadas").classes(
                    "text-positive text-bold"
                )
                ui.label(f"{len(plano.ignoradas)} ignoradas").classes("text-grey-7")
                ui.label(f"({plano.linhas_lidas} linhas lidas)").classes(
                    "text-grey-6 text-xs"
                )

            for motivo, linhas in plano.por_motivo().items():
                with ui.expansion(f"{motivo} — {len(linhas)}").classes("w-full"):
                    ui.label(
                        ", ".join(f"{l.ni} (linha {l.linha_num})" for l in linhas[:50])
                        + (" …" if len(linhas) > 50 else "")
                    ).classes("text-xs text-grey-7")

    def _render_acoes(self, rodape, dialogo, plano) -> None:
        with rodape:
            if plano.ignoradas:
                ui.button(
                    "Baixar ignoradas (CSV)", icon="download",
                    on_click=lambda: ui.download(
                        resultados.ignoradas_para_csv(plano),
                        f"resultados_ignorados_{datetime.now():%Y%m%d_%H%M}.csv",
                        "text/csv",
                    ),
                ).props("flat color=secondary")
            ui.button("Cancelar", on_click=dialogo.close).props("flat")
            ui.button(
                f"Gravar {len(plano.aplicaveis)}", icon="save",
                on_click=lambda: self._confirmar_resultados(dialogo, plano),
            ).props("color=primary").set_enabled(bool(plano.aplicaveis))

    def _confirmar_resultados(self, dialogo, plano) -> None:
        try:
            n = db.gravar_resultados(self.con, plano.registros())
        except Exception as e:  # noqa: BLE001 — erro de banco vira aviso, não stack trace
            ui.notify(f"Falha ao gravar resultados: {e}", type="negative", timeout=8000)
            return
        dialogo.close()
        # n < esperado significa que outro import gravou antes (guarda
        # data_resultado IS NULL) — reportar o número real, não o pretendido.
        extra = "" if n == len(plano.aplicaveis) else (
            f" ({len(plano.aplicaveis) - n} já haviam sido gravadas por outro import)"
        )
        ui.notify(f"{n} resultado(s) gravado(s).{extra}", type="positive")
        self.refresh()

    # -- export ------------------------------------------------------------ #
    def exportar(self, tab: FaseTab, formato: str) -> None:
        """Exporta a visão da aba (filtro + fase + ordenação) em xlsx/csv."""
        where, params = tab._where_params()
        rows = db.listar_amostras(self.con, where=where, params=params)
        if not rows:
            ui.notify("Nada para exportar na visão atual.", type="warning")
            return

        nome_aba = tab.fase
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
        """Após uma ação/filtro: recarrega só a aba VISÍVEL e atualiza os cards.

        As demais abas são marcadas como desatualizadas e só recarregam quando o
        usuário as abre (lazy) — evita re-consultar as 5 abas a cada operação.
        """
        for tab in self.tabs.values():
            tab.invalidar()
        if self._fase_ativa in self.tabs:
            self.tabs[self._fase_ativa].garantir_carregado()
        self._atualizar_cards()

    def _atualizar_cards(self) -> int:
        """Atualiza os cards de métrica (1 query) e devolve o total filtrado."""
        where, params = self.filtro_where_params()
        cont = db.contagens_por_fase(self.con, where=where, params=params)
        total = cont["total"]
        self._cards["total"].text = str(total)
        for chave in _FASES_COM_CARD:
            self._cards[chave].text = str(cont[chave])
            pct = (cont[chave] / total * 100) if total else 0
            self._cards[f"{chave}_pct"].text = f"{pct:.0f}% do total"
        return total

    def _on_tab_change(self, e) -> None:
        """Carrega sob demanda a aba recém-aberta (lazy).

        O on_change dispara também durante a construção (quando tab_panels define
        a aba inicial), antes de self.tabs existir — daí o guard 'fase in self.tabs'.
        O load inicial da Geral é feito explicitamente no fim de construir().
        """
        fase = _TAB_FASE.get(e.value)
        if fase and fase in self.tabs:
            self._fase_ativa = fase
            self.tabs[fase].garantir_carregado()

    def aplicar_filtros(self) -> None:
        """Lê os controles, atualiza o estado e recarrega tudo."""
        self.f_ano = self._ctl_ano.value or None
        self.f_municipio = self._ctl_municipio.value or None
        self.f_busca_ni = (self._ctl_busca.value or "").strip()
        flags = list(self._ctl_flags.value or [])
        self.f_flags = flags
        self.f_sorotipo = self._ctl_sorotipo.value or None
        self.refresh()

    def limpar_filtros(self) -> None:
        self._ctl_ano.value = None
        self._ctl_municipio.value = None
        self._ctl_busca.value = ""
        self._ctl_flags.value = []
        self._ctl_sorotipo.value = None
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
                self._ctl_sorotipo = ui.select(
                    {
                        **{s: s.replace("DEN", "DENV-") for s in db.SOROTIPOS},
                        db.SOROTIPO_NAO_DETECTADO: "Não detectado",
                    },
                    label="Sorotipo", clearable=True,
                ).props("dense").classes("w-40")

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

        # flex-wrap: com uma etapa a mais os cards não cabem numa linha só.
        with ui.row().classes("gap-4 q-mb-md flex-wrap"):
            self._card("Total", "total", "#607d8b")
            for fase in _FASES_COM_CARD:
                self._card(
                    db.ETAPA_POR_CHAVE[fase].label_aba
                    if fase in db.ETAPA_POR_CHAVE else db.LABEL_FASE[fase],
                    fase, db.COR_FASE[fase], com_pct=True,
                )

        self._montar_filtros()

        # Abas e painéis derivados de _ABAS (que vem de db.ETAPAS_DEF).
        objetos_tab = {}
        with ui.tabs(on_change=self._on_tab_change).classes("w-full") as tabs:
            for fase, rotulo in _ABAS:
                objetos_tab[fase] = ui.tab(rotulo)

        with ui.tab_panels(tabs, value=objetos_tab[_FASE_GERAL]).classes("w-full"):
            for fase, _ in _ABAS:
                with ui.tab_panel(objetos_tab[fase]):
                    self.tabs[fase] = FaseTab(self, fase)

        # Load inicial: só a aba visível (Geral) consulta o banco; as outras
        # carregam ao serem abertas (lazy). Os cards trazem o total numa query.
        self.tabs[_FASE_GERAL].garantir_carregado()
        total = self._atualizar_cards()
        if total == 0:
            ui.notify(
                "Importando dados pela primeira vez — aguarde ~1 min e recarregue a página.",
                type="info",
                timeout=0,
            )


def main() -> None:
    auth.build_login_page()

    @ui.page("/")
    def index():
        if not auth.is_authenticated():
            ui.navigate.to("/login")
            return
        t0 = time.perf_counter()
        tracker = App()                       # abre conexão ao Neon
        t_conn = time.perf_counter()
        # Fecha a conexão psycopg2 quando o client é descartado (evita vazar
        # conexões no Neon). on_delete sobrevive a reconexões de websocket.
        ui.context.client.on_delete(lambda *_: tracker.close())
        tracker.construir(
            logout_callback=auth.logout if auth.AUTH_ENABLED else None
        )
        t_end = time.perf_counter()
        # Diagnóstico de latência server-side (ver logs do Render):
        #   connect = abrir conexão ao Neon (inclui acordar se suspenso)
        #   build   = queries do page load + montagem da UI
        print(f"[perf] connect {(t_conn - t0) * 1000:.0f}ms · "
              f"build {(t_end - t_conn) * 1000:.0f}ms", flush=True)

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
