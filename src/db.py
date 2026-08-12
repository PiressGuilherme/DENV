"""Camada de banco — PostgreSQL via Neon (DATABASE_URL obrigatório)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

import psycopg2
import psycopg2.extras

_DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# --------------------------------------------------------------------------- #
# Wrapper de conexão                                                            #
# --------------------------------------------------------------------------- #


class _Conn:
    """Thin wrapper sobre psycopg2 que expõe con.execute() como o sqlite3 faz.

    Cada execute() cria um cursor novo (evita conflito entre queries aninhadas).
    Usa RealDictCursor — row["campo"] funciona em todo o código.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or None)
        return cur

    def executar_lote(self, sql: str, seq_params, page_size: int = 500) -> None:
        """Executa o mesmo SQL para muitos params agrupando em poucas viagens de rede.

        Essencial para o import: 5506 INSERTs num laço = 5506 round-trips até o
        Neon (~minutos). Com execute_batch isso vira ~12 viagens (~segundos).
        """
        cur = self._conn.cursor()
        psycopg2.extras.execute_batch(cur, sql, seq_params, page_size=page_size)

    def executar_values(self, sql: str, seq_params, template: Optional[str] = None) -> int:
        """Executa UM comando com muitas tuplas via VALUES, devolvendo o rowcount.

        Diferente de executar_lote (que roda N comandos e perde o rowcount real),
        aqui tudo vira um único statement — então cur.rowcount é exato. É o que
        permite reportar com honestidade quantas linhas o import de resultados
        realmente gravou quando o UPDATE tem cláusula de guarda.
        """
        cur = self._conn.cursor()
        psycopg2.extras.execute_values(cur, sql, list(seq_params), template=template)
        return cur.rowcount

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# --------------------------------------------------------------------------- #
# Schema                                                                        #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Registro de etapas — fonte ÚNICA da verdade do fluxo                          #
# --------------------------------------------------------------------------- #
# Tudo que descreve uma etapa (coluna, data, pré-requisito, rótulo, cor) mora
# aqui. FASES, ETAPAS, os rótulos da UI e as cláusulas SQL são DERIVADOS deste
# registro — adicionar uma etapa é acrescentar uma linha nesta tupla, não editar
# sete dicionários paralelos espalhados por db.py/app.py/export.py.


@dataclass(frozen=True)
class Etapa:
    """Uma etapa do fluxo de reprocesso.

    Attributes:
        chave: identificador e nome da coluna 0/1 em `amostras`.
        label: rótulo PT-BR no singular (badge, export, botões).
        label_aba: rótulo no plural (aba e card de métrica).
        cor: cor hex do badge/card.
        coluna_data: coluna TIMESTAMP que registra quando a etapa foi marcada.
            Explícita porque a convenção não é uniforme (pcr_feito -> data_pcr).
        label_data: cabeçalho da coluna de data no export. Explícito para manter
            'Data PCR' (e não 'Data PCR feito') em planilhas já em uso.
        prerequisito: etapa que precisa estar marcada antes (avanço estrito).
        exclusiva: se True, marcar esta etapa REMOVE a amostra da aba da etapa
            anterior. Se False, a amostra permanece visível também na anterior —
            é o caso de 'sequenciado', que é um recorte de 'pcr_feito' e não um
            sucessor: a aba PCR feito continua sendo onde os resultados de PCR
            são consultados, mesmo depois do envio para sequenciamento.
    """

    chave: str
    label: str
    label_aba: str
    cor: str
    coluna_data: str
    label_data: str
    prerequisito: Optional[str]
    exclusiva: bool = True

    @property
    def coluna(self) -> str:
        return self.chave


ETAPAS_DEF: tuple[Etapa, ...] = (
    Etapa("coletada",    "Coletada",    "Coletadas",    "#2196f3",
          "data_coletada",    "Data Coletada",    None),
    Etapa("extraida",    "Extraída",    "Extraídas",    "#ff9800",
          "data_extraida",    "Data Extraída",    "coletada"),
    Etapa("pcr_feito",   "PCR feito",   "PCR feito",    "#4caf50",
          "data_pcr",         "Data PCR",         "extraida"),
    Etapa("sequenciado", "Sequenciada", "Sequenciadas", "#7e57c2",
          "data_sequenciado", "Data Sequenciada", "pcr_feito", exclusiva=False),
)

ETAPA_POR_CHAVE: dict[str, Etapa] = {e.chave: e for e in ETAPAS_DEF}

# Etapa em que os resultados de PCR são produzidos. Define onde o botão de
# import aparece e a partir de qual fase as colunas de Ct fazem sentido.
ETAPA_RESULTADO = "pcr_feito"

# Fases cujas amostras já passaram pela PCR — logo, exibem Ct/sorotipo.
FASES_COM_RESULTADO: frozenset[str] = frozenset(
    e.chave
    for e in ETAPAS_DEF[next(i for i, e in enumerate(ETAPAS_DEF)
                             if e.chave == ETAPA_RESULTADO):]
)

# Fases "virtuais": não são etapas marcáveis, mas aparecem como aba/card/badge.
FASE_PENDENTE = "pendente"
FASE_REJEITADA = "rejeitada"

_COR_PENDENTE = "#9e9e9e"
_COR_REJEITADA = "#e53935"

# --------------------------------------------------------------------------- #
# Sorotipos / resultados de PCR                                                 #
# --------------------------------------------------------------------------- #
# Valores de Ct (threshold cycle). NULL = sorotipo não testado (não veio no arquivo).
# -1.0 = "não detectado" (veio no arquivo com Ct "-").
# `data_resultado` é o que define "já tem resultado" de forma inequívoca: um Ct
# nulo sozinho é ambíguo (pode ser negativo OU nunca preenchido).

SOROTIPOS: tuple[str, ...] = ("DEN1", "DEN2", "DEN3", "DEN4")

# Opção de filtro para "tem resultado, mas nenhum sorotipo detectado".
SOROTIPO_NAO_DETECTADO = "__nd__"


def coluna_ct(sorotipo: str) -> str:
    """Nome da coluna de Ct de um sorotipo ('DEN1' -> 'den1_ct')."""
    return f"{sorotipo.lower()}_ct"


COLUNAS_CT: tuple[str, ...] = tuple(coluna_ct(s) for s in SOROTIPOS)

# Controle Interno (CI) - duas colunas, uma por arquivo de corrida
COLUNAS_CI: tuple[str, ...] = ("ci_1_4_ct", "ci_2_3_ct")

# Faixa plausível de Ct numa PCR em tempo real. Fora disso o valor é recusado
# (quase sempre é erro de digitação ou coluna trocada na planilha).
# -1.0 é valor sentinela para "não detectado" (Ct lido como "-" na planilha).
CT_MIN = -1.0
CT_MAX = 50.0

# Casas decimais de Ct guardadas no banco (NUMERIC(6,2)).
CT_DECIMAIS = 2


def mesmo_ct(a, b) -> bool:
    """True se dois valores de Ct são o mesmo na precisão em que são guardados.

    O parser entrega o Ct cru do termociclador (18.746) e a coluna NUMERIC(6,2)
    guarda 18.75. Comparar os dois diretamente marcaria como conflito toda
    reimportação do mesmo arquivo — 202 falsos conflitos numa corrida de 94
    amostras. A comparação é feita na precisão do banco, que é a única em que os
    dois valores são comparáveis.
    """
    if a is None or b is None:
        return a is None and b is None
    return _quantizar_ct(a) == _quantizar_ct(b)


def _quantizar_ct(valor) -> Decimal:
    """Arredonda um Ct como o PostgreSQL faz ao gravar em NUMERIC(6,2).

    ROUND_HALF_UP e não o round() do Python: o padrão do Python é bankers'
    rounding, que leva 14.105 para 14.10 enquanto o Postgres grava 14.11. A
    diferença só aparece no empate exato (.xx5), mas isso bastava para 14 dos
    202 campos continuarem marcados como conflito numa reimportação idêntica.
    """
    return Decimal(str(valor)).quantize(
        Decimal(1).scaleb(-CT_DECIMAIS), rounding=ROUND_HALF_UP
    )

CAMPOS_REPROCESSO = (
    *(e.chave for e in ETAPAS_DEF),
    *(e.coluna_data for e in ETAPAS_DEF),
    "obs_reprocesso", "rejeitada", "motivo_rejeicao", "data_rejeicao",
    *COLUNAS_CT, "data_resultado",
)

CAMPOS_DESCRITIVOS = (
    "prefixo", "numero_sequencial", "ano_verdade",
    "ni_original", "ni_ano", "requisicao", "municipio",
    "data_coleta", "data_sintomas", "caso", "n_origem", "flags",
)

# Colunas adicionadas após a 1ª versão do schema (migração leve para bancos antigos).
_COLUNAS_MIGRACAO = {
    "rejeitada":        "INTEGER NOT NULL DEFAULT 0",
    "motivo_rejeicao":  "TEXT",
    "data_rejeicao":    "TIMESTAMP",
    "sequenciado":      "INTEGER NOT NULL DEFAULT 0",
    "data_sequenciado": "TIMESTAMP",
    **{c: "NUMERIC(6,2)" for c in COLUNAS_CT},
    **{c: "NUMERIC(6,2)" for c in COLUNAS_CI},
    "data_resultado":   "TIMESTAMP",
}

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS amostras (
        chave               TEXT PRIMARY KEY,
        prefixo             TEXT NOT NULL,
        numero_sequencial   INTEGER NOT NULL,
        ano_verdade         INTEGER NOT NULL,
        ni_original         TEXT,
        ni_ano              INTEGER,
        requisicao          TEXT,
        municipio           TEXT,
        data_coleta         DATE,
        data_sintomas       DATE,
        caso                TEXT,
        coletada            INTEGER NOT NULL DEFAULT 0,
        extraida            INTEGER NOT NULL DEFAULT 0,
        pcr_feito           INTEGER NOT NULL DEFAULT 0,
        sequenciado         INTEGER NOT NULL DEFAULT 0,
        data_coletada       TIMESTAMP,
        data_extraida       TIMESTAMP,
        data_pcr            TIMESTAMP,
        data_sequenciado    TIMESTAMP,
        obs_reprocesso      TEXT,
        rejeitada           INTEGER NOT NULL DEFAULT 0,
        motivo_rejeicao     TEXT,
        data_rejeicao       TIMESTAMP,
        den1_ct             NUMERIC(6,2),
        den2_ct             NUMERIC(6,2),
        den3_ct             NUMERIC(6,2),
        den4_ct             NUMERIC(6,2),
        ci_ct               NUMERIC(6,2),
        data_resultado      TIMESTAMP,
        n_origem            INTEGER NOT NULL DEFAULT 1,
        flags               TEXT DEFAULT '',
        importado_em        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        atualizado_em       TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ordem    ON amostras (ano_verdade, prefixo, numero_sequencial)",
    "CREATE INDEX IF NOT EXISTS idx_municipio ON amostras (municipio)",
    "CREATE INDEX IF NOT EXISTS idx_flags    ON amostras (flags)",
    """
    CREATE TABLE IF NOT EXISTS eventos (
        id          BIGSERIAL PRIMARY KEY,
        chave       TEXT NOT NULL REFERENCES amostras(chave) ON UPDATE CASCADE,
        campo       TEXT NOT NULL,
        valor_novo  TEXT,
        em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

ORDER_BY_CANONICO = "ano_verdade ASC, prefixo ASC, numero_sequencial ASC"

def _clausula_da_etapa(indice: int, etapa: Etapa) -> str:
    """Cláusula SQL que define a aba de uma etapa.

    A amostra sai da aba quando alcança a PRÓXIMA etapa exclusiva — etapas não
    exclusivas (sequenciado) não removem a amostra da aba anterior, por isso são
    ignoradas na busca pelo sucessor.
    """
    partes = [f"{etapa.coluna} = 1"]
    sucessora = next((e for e in ETAPAS_DEF[indice + 1:] if e.exclusiva), None)
    if sucessora is not None:
        partes.append(f"{sucessora.coluna} = 0")
    partes.append("rejeitada = 0")
    return " AND ".join(partes)


# Cláusula SQL de cada fase — usada pelas abas e pelos contadores.
# ATENÇÃO: NÃO é uma partição. 'sequenciado' é um SUBCONJUNTO de 'pcr_feito'
# (exclusiva=False), logo somar todas as fases conta essas amostras duas vezes.
# Para invariantes de partição use FASES_EXCLUSIVAS.
FASES: dict[str, str] = {
    FASE_PENDENTE: f"{ETAPAS_DEF[0].coluna} = 0 AND rejeitada = 0",
    **{e.chave: _clausula_da_etapa(i, e) for i, e in enumerate(ETAPAS_DEF)},
    FASE_REJEITADA: "rejeitada = 1",
}

# Subconjunto disjunto que cobre 100% das amostras — cada amostra cai em
# exatamente uma destas. É o conjunto correto para checar a invariante de soma.
FASES_EXCLUSIVAS: dict[str, str] = {
    fase: clausula
    for fase, clausula in FASES.items()
    if fase not in ETAPA_POR_CHAVE or ETAPA_POR_CHAVE[fase].exclusiva
}

ETAPAS: tuple[str, ...] = tuple(e.chave for e in ETAPAS_DEF)

# Rótulos e cores de TODAS as fases (etapas + virtuais), na ordem do fluxo.
LABEL_FASE: dict[str, str] = {
    FASE_PENDENTE: "Pendente",
    **{e.chave: e.label for e in ETAPAS_DEF},
    FASE_REJEITADA: "Rejeitada",
}

COR_FASE: dict[str, str] = {
    FASE_PENDENTE: _COR_PENDENTE,
    **{e.chave: e.cor for e in ETAPAS_DEF},
    FASE_REJEITADA: _COR_REJEITADA,
}

_DATA_DE = {e.chave: e.coluna_data for e in ETAPAS_DEF}

_PREREQUISITO = {e.chave: e.prerequisito for e in ETAPAS_DEF}


def fase_da_linha(r) -> str:
    """Fase "mais avançada" de uma linha — usada para o badge.

    Fonte única da derivação em Python (app.py e export.py delegam aqui).
    Rejeição vence tudo; depois vale a etapa mais avançada já marcada. Uma
    amostra sequenciada exibe o badge 'Sequenciada' inclusive na aba PCR feito,
    o que deixa visível de relance o que já seguiu para sequenciamento.
    """
    if r["rejeitada"]:
        return FASE_REJEITADA
    for etapa in reversed(ETAPAS_DEF):
        if r[etapa.coluna]:
            return etapa.chave
    return FASE_PENDENTE

MOTIVOS_REJEICAO = ("Volume Insuficiente", "Não Encontrada")

# --------------------------------------------------------------------------- #
# Conexão                                                                       #
# --------------------------------------------------------------------------- #


def conectar() -> _Conn:
    """Abre uma conexão PostgreSQL usando DATABASE_URL."""
    return _Conn(psycopg2.connect(_DATABASE_URL))


# --------------------------------------------------------------------------- #
# Schema: criação e migrações                                                   #
# --------------------------------------------------------------------------- #


def _colunas_existentes(con: _Conn, tabela: str = "amostras") -> set[str]:
    """Nomes das colunas da tabela no schema corrente."""
    return {
        r["column_name"]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = CURRENT_SCHEMA()",
            (tabela,),
        ).fetchall()
    }


def _migrar(con: _Conn, *, commit: bool = True) -> list[str]:
    """Adiciona colunas novas a bancos pré-existentes (idempotente).

    Consulta information_schema PRIMEIRO e só emite ALTER TABLE para o que
    realmente falta. Em regime normal isso é ZERO ALTERs — apenas um SELECT
    barato, sem lock. Essa é a diferença que permite chamar a migração no boot
    sem repetir o incidente do commit ed5160a, onde o ALTER incondicional a cada
    deploy pegava ACCESS EXCLUSIVE e travava a tabela sob deploys sobrepostos.

    Returns:
        Lista das colunas efetivamente adicionadas (vazia no caso comum).
    """
    existentes = _colunas_existentes(con)
    faltando = [c for c in _COLUNAS_MIGRACAO if c not in existentes]
    for coluna in faltando:
        # ADD COLUMN com default constante é metadata-only no PG 11+ (não
        # reescreve a tabela), logo o lock dura milissegundos.
        con.execute(
            f"ALTER TABLE amostras ADD COLUMN IF NOT EXISTS {coluna} "
            f"{_COLUNAS_MIGRACAO[coluna]}"
        )
    if commit:
        con.commit()
    return faltando


def _reclassificar_2026(con: _Conn) -> int:
    """Reconcilia bancos já populados com a regra de reclassificação 2026.

    As 73 amostras D (ni_ano=2026, nº 1–976) importadas antes da regra estão
    como D{n}/25 (ano_verdade=2025). Move para D{n}/26 preservando progresso.
    ON UPDATE CASCADE em eventos.chave elimina a necessidade de desabilitar FKs.
    Idempotente: só age sobre linhas ainda não reclassificadas.
    """
    from src.parsing import calcular_flags, montar_chave
    from src.parsing import reclassificar_2026 as _eh_2026

    candidatas = con.execute(
        "SELECT chave, prefixo, numero_sequencial, ni_ano, ano_verdade, "
        "data_coleta, data_sintomas "
        "FROM amostras WHERE prefixo = %s AND ni_ano = %s "
        "AND numero_sequencial BETWEEN %s AND %s AND ano_verdade != %s",
        ("D", 2026, 1, 976, 2026),
    ).fetchall()

    if not candidatas:
        return 0

    movidas = 0
    for r in candidatas:
        if not _eh_2026(r["prefixo"], r["numero_sequencial"], r["ni_ano"]):
            continue
        nova_chave = montar_chave(r["prefixo"], r["numero_sequencial"], 2026)
        existe = con.execute(
            "SELECT 1 FROM amostras WHERE chave = %s", (nova_chave,)
        ).fetchone()
        if existe and nova_chave != r["chave"]:
            continue
        flags = calcular_flags(
            ni_ano=r["ni_ano"],
            ano_verdade_=2026,
            data_coleta=_parse_iso(r["data_coleta"]),
            data_sintomas=_parse_iso(r["data_sintomas"]),
        )
        con.execute(
            "UPDATE amostras SET chave = %s, ano_verdade = 2026, flags = %s, "
            "atualizado_em = CURRENT_TIMESTAMP WHERE chave = %s",
            (nova_chave, flags, r["chave"]),
        )
        movidas += 1
    con.commit()
    return movidas


def _parse_iso(valor) -> Optional["datetime"]:
    from datetime import datetime
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


# ID arbitrário para o advisory lock que serializa a criação de schema entre
# processos/threads concorrentes (deploys sobrepostos do Render).
_SCHEMA_LOCK_ID = 728193


def criar_schema(con: _Conn) -> None:
    """Cria tabelas e índices (idempotente), serializado por advisory lock.

    Roda _migrar DENTRO da mesma transação do advisory lock. Bancos já povoados
    não recebem colunas novas via CREATE TABLE IF NOT EXISTS, então a migração é
    necessária — mas só emite ALTER para o que falta (ver _migrar), o que a torna
    um no-op silencioso em regime normal e evita o lock que motivou o ed5160a.

    Usa pg_advisory_xact_lock (escopo de transação) — serializa criação
    concorrente e é seguro com o pooler do Neon (PgBouncer em modo transação),
    pois é liberado no commit, sem precisar de unlock explícito de sessão.
    """
    con.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
    for stmt in _SCHEMA:
        con.execute(stmt.strip())
    adicionadas = _migrar(con, commit=False)
    con.commit()  # aplica schema + migração e libera o advisory lock
    if adicionadas:
        print(f"[schema] colunas adicionadas: {', '.join(adicionadas)}", flush=True)
    _reclassificar_2026(con)


def init_db() -> _Conn:
    """Conveniência: conecta e garante o schema."""
    con = conectar()
    criar_schema(con)
    return con


# --------------------------------------------------------------------------- #
# Queries                                                                       #
# --------------------------------------------------------------------------- #


def listar_amostras(
    con: _Conn,
    *,
    order_by: str = ORDER_BY_CANONICO,
    where: Optional[str] = None,
    params: Iterable = (),
) -> list:
    sql = "SELECT * FROM amostras"
    if where:
        sql += f" WHERE {where}"
    sql += f" ORDER BY {order_by}"
    return con.execute(sql, tuple(params)).fetchall()


def registrar_evento(
    con: _Conn, chave: str, campo: str, valor_novo: Optional[str]
) -> None:
    con.execute(
        "INSERT INTO eventos (chave, campo, valor_novo) VALUES (%s, %s, %s)",
        (chave, campo, str(valor_novo) if valor_novo is not None else None),
    )


def contar(con: _Conn, where: Optional[str] = None, params: Iterable = ()) -> int:
    sql = "SELECT COUNT(*) AS n FROM amostras"
    if where:
        sql += f" WHERE {where}"
    return int(con.execute(sql, tuple(params)).fetchone()["n"])


def valores_distintos(con: _Conn, coluna: str) -> list:
    """Valores distintos não-nulos de uma coluna, ordenados (para dropdowns)."""
    permitidas = {"ano_verdade", "municipio", "caso", "prefixo"}
    if coluna not in permitidas:
        raise ValueError(f"coluna não permitida para distinct: {coluna!r}")
    rows = con.execute(
        f"SELECT DISTINCT {coluna} FROM amostras "
        f"WHERE {coluna} IS NOT NULL AND {coluna}::text != '' ORDER BY {coluna}"
    ).fetchall()
    return [r[coluna] for r in rows]


def construir_filtro(
    *,
    ano: Optional[int] = None,
    municipio: Optional[str] = None,
    busca_ni: Optional[str] = None,
    flags_qualquer: Optional[Iterable[str]] = None,
    com_flags: Optional[bool] = None,
    sorotipo: Optional[str] = None,
    com_resultado: Optional[bool] = None,
) -> tuple[Optional[str], list]:
    """Monta (where, params) do filtro global.

    Args:
        sorotipo: 'DEN1'..'DEN4' para detectados, ou SOROTIPO_NAO_DETECTADO
            para amostras com resultado gravado e nenhum Ct.
        com_resultado: True/False para ter/não ter resultado gravado.
    """
    clausulas: list[str] = []
    params: list = []

    if ano is not None:
        clausulas.append("ano_verdade = %s")
        params.append(ano)
    if municipio:
        clausulas.append("municipio = %s")
        params.append(municipio)
    if busca_ni:
        # ILIKE = LIKE case-insensitive (PostgreSQL). No SQLite o LIKE já era
        # case-insensitive; ILIKE preserva esse comportamento para a busca livre.
        clausulas.append("(ni_original ILIKE %s OR chave ILIKE %s)")
        termo = f"%{busca_ni.strip()}%"
        params.extend([termo, termo])
    if flags_qualquer:
        ors = []
        for f in flags_qualquer:
            ors.append("flags LIKE %s")
            params.append(f"%{f}%")
        if ors:
            clausulas.append("(" + " OR ".join(ors) + ")")
    if com_flags is True:
        clausulas.append("flags != ''")
    elif com_flags is False:
        clausulas.append("flags = ''")

    if sorotipo == SOROTIPO_NAO_DETECTADO:
        # Resultado gravado, porém nenhum sorotipo detectado.
        nenhum = " AND ".join(f"{c} IS NULL" for c in COLUNAS_CT)
        clausulas.append(f"(data_resultado IS NOT NULL AND {nenhum})")
    elif sorotipo:
        if sorotipo not in SOROTIPOS:
            raise ValueError(f"sorotipo inválido: {sorotipo!r} (use {list(SOROTIPOS)})")
        clausulas.append(f"{coluna_ct(sorotipo)} IS NOT NULL")

    if com_resultado is True:
        clausulas.append("data_resultado IS NOT NULL")
    elif com_resultado is False:
        clausulas.append("data_resultado IS NULL")

    where = " AND ".join(clausulas) if clausulas else None
    return where, params


# --------------------------------------------------------------------------- #
# Fluxo de fases (kanban)                                                       #
# --------------------------------------------------------------------------- #


class TransicaoInvalida(Exception):
    pass


def where_por_fase(fase: str) -> str:
    try:
        return FASES[fase]
    except KeyError:
        raise ValueError(f"fase desconhecida: {fase!r} (use {list(FASES)})")


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n)


def avancar_fase(con: _Conn, chaves: Iterable[str], etapa: str) -> int:
    if etapa not in ETAPAS:
        raise ValueError(f"etapa desconhecida: {etapa!r}")
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0

    prereq = _PREREQUISITO[etapa]
    if prereq is not None:
        ph = _placeholders(len(chaves))
        faltando = con.execute(
            f"SELECT COUNT(*) AS n FROM amostras "
            f"WHERE chave IN ({ph}) AND {prereq} = 0",
            chaves,
        ).fetchone()["n"]
        if faltando:
            raise TransicaoInvalida(
                f"{faltando} amostra(s) sem '{prereq}' — não é possível marcar '{etapa}'."
            )

    col_data = _DATA_DE[etapa]
    ph = _placeholders(len(chaves))
    cur = con.execute(
        f"UPDATE amostras "
        f"SET {etapa} = 1, "
        f"    {col_data} = COALESCE({col_data}, CURRENT_TIMESTAMP), "
        f"    atualizado_em = CURRENT_TIMESTAMP "
        f"WHERE chave IN ({ph}) AND {etapa} = 0",
        chaves,
    )
    alteradas = cur.rowcount
    for chave in chaves:
        registrar_evento(con, chave, etapa, "1")
    con.commit()
    return alteradas


def retroceder_fase(con: _Conn, chaves: Iterable[str], etapa: str) -> int:
    if etapa not in ETAPAS:
        raise ValueError(f"etapa desconhecida: {etapa!r}")
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0

    idx = ETAPAS.index(etapa)
    a_limpar = ETAPAS[idx:]
    sets = []
    for e in a_limpar:
        sets.append(f"{e} = 0")
        sets.append(f"{_DATA_DE[e]} = NULL")
    sets.append("atualizado_em = CURRENT_TIMESTAMP")

    ph = _placeholders(len(chaves))
    cur = con.execute(
        f"UPDATE amostras SET {', '.join(sets)} "
        f"WHERE chave IN ({ph}) AND {etapa} = 1",
        chaves,
    )
    alteradas = cur.rowcount
    for chave in chaves:
        registrar_evento(con, chave, etapa, "0")
    con.commit()
    return alteradas


def rejeitar(con: _Conn, chaves: Iterable[str], motivo: str) -> int:
    if motivo not in MOTIVOS_REJEICAO:
        raise ValueError(f"motivo inválido: {motivo!r} (use {list(MOTIVOS_REJEICAO)})")
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0

    ph = _placeholders(len(chaves))
    inelegiveis = con.execute(
        f"SELECT COUNT(*) AS n FROM amostras "
        f"WHERE chave IN ({ph}) AND NOT (coletada = 0 AND rejeitada = 0)",
        chaves,
    ).fetchone()["n"]
    if inelegiveis:
        raise TransicaoInvalida(
            f"{inelegiveis} amostra(s) não estão pendentes — só é possível "
            f"rejeitar amostras pendentes."
        )

    cur = con.execute(
        f"UPDATE amostras "
        f"SET rejeitada = 1, motivo_rejeicao = %s, "
        f"    data_rejeicao = CURRENT_TIMESTAMP, atualizado_em = CURRENT_TIMESTAMP "
        f"WHERE chave IN ({ph}) AND rejeitada = 0",
        [motivo, *chaves],
    )
    alteradas = cur.rowcount
    for chave in chaves:
        registrar_evento(con, chave, "rejeitada", motivo)
    con.commit()
    return alteradas


def reverter_rejeicao(con: _Conn, chaves: Iterable[str]) -> int:
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0
    ph = _placeholders(len(chaves))
    cur = con.execute(
        f"UPDATE amostras "
        f"SET rejeitada = 0, motivo_rejeicao = NULL, data_rejeicao = NULL, "
        f"    atualizado_em = CURRENT_TIMESTAMP "
        f"WHERE chave IN ({ph}) AND rejeitada = 1",
        chaves,
    )
    alteradas = cur.rowcount
    for chave in chaves:
        registrar_evento(con, chave, "rejeitada", "0")
    con.commit()
    return alteradas


def contagens_por_fase(
    con: _Conn, where: Optional[str] = None, params: Iterable = ()
) -> dict[str, int]:
    """Contagem de cada fase + total numa ÚNICA query (COUNT FILTER).

    Antes eram 6 viagens ao banco (uma por fase + total). As cláusulas de FASES
    são estáticas (sem placeholders), então entram direto no FILTER; os params
    pertencem só ao WHERE do filtro global, aplicado uma vez.
    """
    selects = [
        f"COUNT(*) FILTER (WHERE {clausula}) AS {fase}"
        for fase, clausula in FASES.items()
    ]
    selects.append("COUNT(*) AS total")
    sql = f"SELECT {', '.join(selects)} FROM amostras"
    if where:
        sql += f" WHERE {where}"
    row = con.execute(sql, tuple(params)).fetchone()
    return {chave: int(row[chave]) for chave in (*FASES.keys(), "total")}


def _combinar_where(*clausulas: Optional[str]) -> Optional[str]:
    partes = [f"({c})" for c in clausulas if c]
    return " AND ".join(partes) if partes else None


# --------------------------------------------------------------------------- #
# Resultados de PCR (Ct por sorotipo)                                           #
# --------------------------------------------------------------------------- #

# Colunas necessárias para resolver NI -> chave e triar o import.
_COLUNAS_INDICE = (
    "chave", "prefixo", "numero_sequencial", "ano_verdade", "ni_original",
    "pcr_feito", "sequenciado", "rejeitada", "data_resultado",
)


def indice_para_resultados(con: _Conn) -> list[dict]:
    """Universo mínimo para resolver NIs do arquivo de resultados.

    Uma query só (~5,5 mil linhas, barato) em vez de um SELECT por NI — contra o
    Neon, N round-trips seriam o gargalo do import.
    """
    return [
        dict(r)
        for r in con.execute(
            f"SELECT {', '.join(_COLUNAS_INDICE)} FROM amostras"
        ).fetchall()
    ]


def gravar_resultados(con: _Conn, registros: Iterable[dict]) -> int:
    """Grava Ct por sorotipo, sem jamais sobrescrever resultado existente.

    Args:
        registros: dicts com 'chave' + as colunas de COLUNAS_CT (valores None
            para sorotipo não detectado).

    Returns:
        Quantidade de amostras efetivamente gravadas.

    A cláusula `data_resultado IS NULL` é a guarda transacional contra
    sobrescrita: mesmo que dois usuários importem em paralelo, o segundo não
    sobrepõe o primeiro — e o rowcount devolvido reflete o que de fato mudou,
    não o que se pretendia mudar.
    """
    registros = list(registros)
    if not registros:
        return 0

    colunas = ", ".join(COLUNAS_CT)
    sets = ", ".join(f"{c} = v.{c}" for c in COLUNAS_CT)
    # Cast explícito: VALUES sem tipo chega como texto e o UPDATE falharia.
    casts = ", ".join(["%s"] + ["%s::numeric"] * len(COLUNAS_CT))
    sql = (
        f"UPDATE amostras AS a SET {sets}, "
        f"    data_resultado = CURRENT_TIMESTAMP, "
        f"    atualizado_em = CURRENT_TIMESTAMP "
        f"FROM (VALUES %s) AS v(chave, {colunas}) "
        f"WHERE a.chave = v.chave AND a.data_resultado IS NULL"
    )
    tuplas = [
        (r["chave"], *(r.get(c) for c in COLUNAS_CT)) for r in registros
    ]
    gravadas = con.executar_values(sql, tuplas, template=f"({casts})")

    for r in registros:
        valores = ";".join(
            "" if r.get(c) is None else str(r[c]) for c in COLUNAS_CT
        )
        registrar_evento(con, r["chave"], "resultado_den", valores)
    con.commit()
    return gravadas


# --------------------------------------------------------------------------- #
# Resultados do Termociclador (import por campo com conflitos)                  #
# --------------------------------------------------------------------------- #

from dataclasses import dataclass
from typing import Optional


@dataclass
class ConflitoCampo:
    """Representa um conflito de valor em um campo específico."""
    chave: str
    campo: str
    valor_atual: Optional[float]
    valor_novo: Optional[float]


@dataclass
class ResultadoGravacaoTermociclador:
    """Resultado da gravação de resultados do termociclador."""
    gravados: int = 0           # número de amostras com pelo menos um campo gravado
    campos_gravados: int = 0    # total de campos individuais gravados
    conflitos: list[ConflitoCampo] = None  # campos com valor_atual != valor_novo (não-None)
    nao_encontradas: list[str] = None      # chaves que não existem no banco
    ano_ambiguo: list[str] = None          # sample IDs que casam com múltiplos anos

    def __post_init__(self):
        if self.conflitos is None:
            self.conflitos = []
        if self.nao_encontradas is None:
            self.nao_encontradas = []
        if self.ano_ambiguo is None:
            self.ano_ambiguo = []


def resolver_amostras_termociclador(
    con: _Conn,
    amostras_termociclador: list[dict],
) -> tuple[dict[str, dict], list[str], list[str]]:
    """
    Resolve amostras do termociclador para chaves do banco.

    Args:
        con: conexão com o banco
        amostras_termociclador: lista de dicts com chaves:
            - prefixo (str, default "D")
            - numero_sequencial (int)
            - ano_verdade (int, opcional)
            - cts: dict com den1_ct, den2_ct, den3_ct, den4_ct, ci_1_4_ct, ci_2_3_ct

    Returns:
        Tupla (amostras_resolvidas, nao_encontradas, ano_ambiguo)
        - amostras_resolvidas: dict {chave_banco: dict_com_cts}
        - nao_encontradas: lista de sample_ids que não casaram com nada
        - ano_ambiguo: lista de sample_ids que casaram com múltiplos anos
    """
    # Busca todas as amostras candidatas do banco (prefixo D)
    rows = con.execute(
        "SELECT chave, prefixo, numero_sequencial, ano_verdade "
        "FROM amostras WHERE prefixo = %s",
        ("D",),
    ).fetchall()

    # Índice por (prefixo, numero_sequencial) -> lista de chaves (pode ter vários anos)
    por_numero: dict[tuple[str, int], list[dict]] = {}
    por_chave: dict[str, dict] = {}
    for r in rows:
        key = (r["prefixo"], r["numero_sequencial"])
        por_numero.setdefault(key, []).append(r)
        por_chave[r["chave"]] = r

    resolvidas = {}
    nao_encontradas = []
    ano_ambiguo = []

    for amp in amostras_termociclador:
        prefixo = amp.get("prefixo", "D")
        numero = amp["numero_sequencial"]
        ano = amp.get("ano_verdade")
        cts = amp["cts"]

        sample_id = f"{prefixo}{numero}" + (f"/{ano % 100:02d}" if ano else "")

        if ano is not None:
            # Busca exata pela chave completa
            chave_esperada = f"{prefixo}{numero}/{ano % 100:02d}"
            if chave_esperada in por_chave:
                resolvidas[chave_esperada] = cts
            else:
                nao_encontradas.append(sample_id)
        else:
            # Busca por prefixo + numero (pode retornar múltiplos anos)
            candidatos = por_numero.get((prefixo, numero), [])
            if not candidatos:
                nao_encontradas.append(sample_id)
            elif len(candidatos) == 1:
                resolvidas[candidatos[0]["chave"]] = cts
            else:
                # Múltiplos anos para o mesmo número - ambíguo
                ano_ambiguo.append(sample_id)

    return resolvidas, nao_encontradas, ano_ambiguo


def gravar_resultados_termociclador(
    con: _Conn,
    amostras_resolvidas: dict[str, dict],
    sobrescrever: Optional[set[tuple[str, str]]] = None,
) -> ResultadoGravacaoTermociclador:
    """
    Grava resultados do termociclador por campo, sem sobrescrever por padrão.

    Args:
        con: conexão com o banco
        amostras_resolvidas: dict {chave: {den1_ct, den2_ct, den3_ct, den4_ct, ci_1_4_ct, ci_2_3_ct}}
        sobrescrever: set de (chave, campo) que o usuário autorizou sobrescrever

    Returns:
        ResultadoGravacaoTermociclador com contadores e conflitos
    """
    if sobrescrever is None:
        sobrescrever = set()

    resultado = ResultadoGravacaoTermociclador()

    # Todas as colunas que podem ser gravadas (DEN1-4 + CI 1-4 + CI 2-3)
    TODOS_CAMPOS = (*COLUNAS_CT, *COLUNAS_CI)

    for chave, cts in amostras_resolvidas.items():
        # Busca valores atuais no banco
        row = con.execute(
            f"SELECT {', '.join(TODOS_CAMPOS)} FROM amostras WHERE chave = %s",
            (chave,),
        ).fetchone()

        if not row:
            resultado.nao_encontradas.append(chave)
            continue

        campos_para_gravar = {}
        conflitos_amostra = []  # para log JSON
        tem_gravacao = False

        for campo in TODOS_CAMPOS:
            valor_novo = cts.get(campo)
            valor_atual = row[campo]

            if valor_novo is None:
                # Não tem valor novo - não faz nada
                continue

            if valor_atual is None:
                # Campo vazio no banco - pode gravar
                campos_para_gravar[campo] = valor_novo
                conflitos_amostra.append({
                    "campo": campo,
                    "valor_atual": None,
                    "valor_novo": valor_novo,
                    "sobrescrito": False,
                })
                tem_gravacao = True
            elif not mesmo_ct(valor_atual, valor_novo):
                # Conflito: valor diferente do existente
                if (chave, campo) in sobrescrever:
                    # Usuário autorizou sobrescrever
                    campos_para_gravar[campo] = valor_novo
                    conflitos_amostra.append({
                        "campo": campo,
                        "valor_atual": valor_atual,
                        "valor_novo": valor_novo,
                        "sobrescrito": True,
                    })
                    tem_gravacao = True
                else:
                    # Conflito não autorizado - registra
                    resultado.conflitos.append(ConflitoCampo(
                        chave=chave,
                        campo=campo,
                        valor_atual=valor_atual,
                        valor_novo=valor_novo,
                    ))
            # Se valor_atual == valor_novo, não faz nada (já está igual)

        if tem_gravacao:
            # Monta UPDATE dinâmico só para os campos que vão mudar
            sets = ", ".join(f"{c} = %s" for c in campos_para_gravar)
            params = list(campos_para_gravar.values()) + [chave]
            con.execute(
                f"UPDATE amostras SET {sets}, "
                f"    data_resultado = CURRENT_TIMESTAMP, "
                f"    atualizado_em = CURRENT_TIMESTAMP "
                f"WHERE chave = %s",
                params,
            )
            resultado.gravados += 1
            resultado.campos_gravados += len(campos_para_gravar)

            # Registra evento ÚNICO por amostra com JSON (economia de linhas em eventos)
            import json
            registrar_evento(
                con, chave, "resultado_termociclador",
                json.dumps({"campos": conflitos_amostra}, ensure_ascii=False)
            )

    con.commit()
    return resultado
