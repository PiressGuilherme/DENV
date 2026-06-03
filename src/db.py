"""Camada de banco — PostgreSQL via Neon (DATABASE_URL obrigatório)."""

from __future__ import annotations

import os
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

CAMPOS_REPROCESSO = (
    "coletada", "extraida", "pcr_feito",
    "data_coletada", "data_extraida", "data_pcr",
    "obs_reprocesso", "rejeitada", "motivo_rejeicao", "data_rejeicao",
)

CAMPOS_DESCRITIVOS = (
    "prefixo", "numero_sequencial", "ano_verdade",
    "ni_original", "ni_ano", "requisicao", "municipio",
    "data_coleta", "data_sintomas", "caso", "n_origem", "flags",
)

# Colunas adicionadas após a 1ª versão do schema (migração leve para bancos antigos).
_COLUNAS_MIGRACAO = {
    "rejeitada":      "INTEGER NOT NULL DEFAULT 0",
    "motivo_rejeicao": "TEXT",
    "data_rejeicao":  "TIMESTAMP",
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
        data_coletada       TIMESTAMP,
        data_extraida       TIMESTAMP,
        data_pcr            TIMESTAMP,
        obs_reprocesso      TEXT,
        rejeitada           INTEGER NOT NULL DEFAULT 0,
        motivo_rejeicao     TEXT,
        data_rejeicao       TIMESTAMP,
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

FASES: dict[str, str] = {
    "pendente":  "coletada = 0 AND rejeitada = 0",
    "coletada":  "coletada = 1 AND extraida = 0 AND rejeitada = 0",
    "extraida":  "extraida = 1 AND pcr_feito = 0 AND rejeitada = 0",
    "pcr_feito": "pcr_feito = 1 AND rejeitada = 0",
    "rejeitada": "rejeitada = 1",
}

ETAPAS = ("coletada", "extraida", "pcr_feito")

_DATA_DE = {
    "coletada": "data_coletada",
    "extraida": "data_extraida",
    "pcr_feito": "data_pcr",
}

_PREREQUISITO = {
    "coletada": None,
    "extraida": "coletada",
    "pcr_feito": "extraida",
}

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


def _migrar(con: _Conn) -> None:
    """Adiciona colunas novas a bancos pré-existentes (idempotente)."""
    for coluna, tipo in _COLUNAS_MIGRACAO.items():
        con.execute(
            f"ALTER TABLE amostras ADD COLUMN IF NOT EXISTS {coluna} {tipo}"
        )
    con.commit()


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


def criar_schema(con: _Conn) -> None:
    """Cria tabelas e índices (idempotente) e roda migrações."""
    for stmt in _SCHEMA:
        con.execute(stmt.strip())
    con.commit()
    _migrar(con)
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
) -> tuple[Optional[str], list]:
    clausulas: list[str] = []
    params: list = []

    if ano is not None:
        clausulas.append("ano_verdade = %s")
        params.append(ano)
    if municipio:
        clausulas.append("municipio = %s")
        params.append(municipio)
    if busca_ni:
        clausulas.append("(ni_original LIKE %s OR chave LIKE %s)")
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
    params = tuple(params)
    out = {}
    for fase, clausula in FASES.items():
        w = _combinar_where(where, clausula)
        out[fase] = contar(con, where=w, params=params)
    out["total"] = contar(con, where=where, params=params)
    return out


def _combinar_where(*clausulas: Optional[str]) -> Optional[str]:
    partes = [f"({c})" for c in clausulas if c]
    return " AND ".join(partes) if partes else None
