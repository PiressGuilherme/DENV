"""Camada de banco — SQLite (Seção 4 do CLAUDE.md).

Responsável por: criar o schema, abrir conexões, e fornecer as queries canônicas
(em especial a ordenação cronológica da Seção 3.3). Nenhuma regra de parsing mora
aqui — isso é de ``parsing.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

# Caminho default do banco (raiz do projeto). Gitignored.
DB_PATH = Path(__file__).resolve().parent.parent / "reprocesso.db"

# Campos que o fluxo de reprocesso controla — NUNCA sobrescritos no reimport
# (Seção 7, passo 7: idempotência).
CAMPOS_REPROCESSO = (
    "coletada",
    "extraida",
    "pcr_feito",
    "data_coletada",
    "data_extraida",
    "data_pcr",
    "obs_reprocesso",
)

# Campos descritivos atualizáveis a cada reimport (vêm da planilha de origem).
CAMPOS_DESCRITIVOS = (
    "prefixo",
    "numero_sequencial",
    "ano_verdade",
    "ni_original",
    "ni_ano",
    "requisicao",
    "municipio",
    "data_coleta",
    "data_sintomas",
    "caso",
    "n_origem",
    "flags",
)

_SCHEMA = """
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

    -- FLUXO DE REPROCESSO (o que o sistema controla)
    coletada            INTEGER NOT NULL DEFAULT 0,
    extraida            INTEGER NOT NULL DEFAULT 0,
    pcr_feito           INTEGER NOT NULL DEFAULT 0,
    data_coletada       TIMESTAMP,
    data_extraida       TIMESTAMP,
    data_pcr            TIMESTAMP,
    obs_reprocesso      TEXT,

    -- METADADOS / AUDITORIA
    n_origem            INTEGER NOT NULL DEFAULT 1,
    flags               TEXT DEFAULT '',
    importado_em        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizado_em       TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ordem ON amostras (ano_verdade, prefixo, numero_sequencial);
CREATE INDEX IF NOT EXISTS idx_municipio ON amostras (municipio);
CREATE INDEX IF NOT EXISTS idx_flags ON amostras (flags);

CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chave       TEXT NOT NULL REFERENCES amostras(chave),
    campo       TEXT NOT NULL,
    valor_novo  TEXT,
    em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Ordenação cronológica canônica (Seção 3.3). numero_sequencial é INTEGER.
ORDER_BY_CANONICO = "ano_verdade ASC, prefixo ASC, numero_sequencial ASC"


def conectar(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Abre uma conexão SQLite com row_factory e foreign keys ligadas."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def criar_schema(con: sqlite3.Connection) -> None:
    """Cria tabelas e índices (idempotente — usa IF NOT EXISTS)."""
    con.executescript(_SCHEMA)
    con.commit()


def init_db(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Conveniência: conecta e garante o schema."""
    con = conectar(db_path)
    criar_schema(con)
    return con


def listar_amostras(
    con: sqlite3.Connection,
    *,
    order_by: str = ORDER_BY_CANONICO,
    where: Optional[str] = None,
    params: Iterable = (),
) -> list[sqlite3.Row]:
    """Lista amostras na ordenação canônica (default) ou filtrada."""
    sql = "SELECT * FROM amostras"
    if where:
        sql += f" WHERE {where}"
    sql += f" ORDER BY {order_by}"
    return con.execute(sql, tuple(params)).fetchall()


def registrar_evento(
    con: sqlite3.Connection,
    chave: str,
    campo: str,
    valor_novo: Optional[str],
) -> None:
    """Grava um evento de auditoria (Seção 9: sempre que tocar dados)."""
    con.execute(
        "INSERT INTO eventos (chave, campo, valor_novo) VALUES (?, ?, ?)",
        (chave, campo, str(valor_novo) if valor_novo is not None else None),
    )


def contar(con: sqlite3.Connection, where: Optional[str] = None, params: Iterable = ()) -> int:
    """Conta amostras (opcionalmente filtradas)."""
    sql = "SELECT COUNT(*) FROM amostras"
    if where:
        sql += f" WHERE {where}"
    return int(con.execute(sql, tuple(params)).fetchone()[0])


# --------------------------------------------------------------------------- #
# Fluxo de reprocesso por fases (kanban)                                       #
# --------------------------------------------------------------------------- #
#
# DECISÃO DO USUÁRIO que SOBREPÕE a Seção 4 do CLAUDE.md ("avisar, não bloquear"):
# o avanço de fase é ESTRITO. Não se pode marcar Extraída sem Coletada, nem PCR
# sem Extraída. A UI e o banco recusam transições fora de ordem.
#
# A fase é DERIVADA dos 3 booleanos (coletada/extraida/pcr_feito). Como o avanço é
# estrito, os estados são aninhados (pcr_feito ⇒ extraida ⇒ coletada), então cada
# amostra cai em EXATAMENTE uma fase — base das abas da UI.

# Etapas em ordem do fluxo. Índice = profundidade.
ETAPAS = ("coletada", "extraida", "pcr_feito")

# Coluna de data de cada etapa.
_DATA_DE = {
    "coletada": "data_coletada",
    "extraida": "data_extraida",
    "pcr_feito": "data_pcr",
}

# Pré-requisito estrito de cada etapa (None = pode marcar livremente).
_PREREQUISITO = {
    "coletada": None,
    "extraida": "coletada",
    "pcr_feito": "extraida",
}

# Mapa fase->cláusula WHERE. Cada amostra cai em exatamente uma (partição completa).
FASES: dict[str, str] = {
    "pendente": "coletada = 0",
    "coletada": "coletada = 1 AND extraida = 0",
    "extraida": "extraida = 1 AND pcr_feito = 0",
    "pcr_feito": "pcr_feito = 1",
}


class TransicaoInvalida(Exception):
    """Tentativa de avançar/retroceder fora da ordem estrita do fluxo."""


def where_por_fase(fase: str) -> str:
    """Devolve a cláusula WHERE de uma fase (para listar_amostras/contar)."""
    try:
        return FASES[fase]
    except KeyError:
        raise ValueError(f"fase desconhecida: {fase!r} (use {list(FASES)})")


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def avancar_fase(
    con: sqlite3.Connection,
    chaves: Iterable[str],
    etapa: str,
) -> int:
    """Marca uma etapa=1 (e grava a data) em lote, com avanço ESTRITO.

    Valida o pré-requisito: toda chave selecionada deve ter a etapa anterior
    concluída. Se alguma não tiver, levanta TransicaoInvalida e NÃO altera nada
    (transação única). Grava um evento por chave.

    Args:
        con: conexão SQLite.
        chaves: chaves das amostras a avançar.
        etapa: "coletada" | "extraida" | "pcr_feito".

    Returns:
        Número de amostras efetivamente alteradas.
    """
    if etapa not in ETAPAS:
        raise ValueError(f"etapa desconhecida: {etapa!r}")
    chaves = list(dict.fromkeys(chaves))  # dedup preservando ordem
    if not chaves:
        return 0

    prereq = _PREREQUISITO[etapa]
    if prereq is not None:
        # Recusa se alguma chave não tem o pré-requisito feito.
        ph = _placeholders(len(chaves))
        faltando = con.execute(
            f"SELECT COUNT(*) FROM amostras "
            f"WHERE chave IN ({ph}) AND {prereq} = 0",
            chaves,
        ).fetchone()[0]
        if faltando:
            raise TransicaoInvalida(
                f"{faltando} amostra(s) sem '{prereq}' — não é possível marcar '{etapa}'."
            )

    col_data = _DATA_DE[etapa]
    ph = _placeholders(len(chaves))
    # Só altera quem ainda não está marcado (idempotente; rowcount = alterações reais).
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


def retroceder_fase(
    con: sqlite3.Connection,
    chaves: Iterable[str],
    etapa: str,
) -> int:
    """Desmarca uma etapa em lote, limpando também as etapas POSTERIORES.

    Consistência estrita: desmarcar 'coletada' também limpa 'extraida' e
    'pcr_feito' (uma amostra não pode estar extraída sem estar coletada). Limpa
    as datas correspondentes. Grava um evento por chave.

    Returns:
        Número de amostras efetivamente alteradas.
    """
    if etapa not in ETAPAS:
        raise ValueError(f"etapa desconhecida: {etapa!r}")
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0

    # Etapa atual + todas as posteriores são zeradas.
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


def contagens_por_fase(con: sqlite3.Connection) -> dict[str, int]:
    """Contadores de cada fase + total (para o cabeçalho de métricas da UI)."""
    out = {fase: contar(con, where=clausula) for fase, clausula in FASES.items()}
    out["total"] = contar(con)
    return out
