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
    "rejeitada",
    "motivo_rejeicao",
    "data_rejeicao",
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

    -- REJEIÇÃO (estado terminal alternativo: amostra não entra no fluxo)
    rejeitada           INTEGER NOT NULL DEFAULT 0,
    motivo_rejeicao     TEXT,
    data_rejeicao       TIMESTAMP,

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


# Colunas adicionadas após a 1ª versão do schema. Migração leve para bancos
# já existentes (CREATE TABLE IF NOT EXISTS não altera tabelas antigas).
_COLUNAS_MIGRACAO = {
    "rejeitada": "INTEGER NOT NULL DEFAULT 0",
    "motivo_rejeicao": "TEXT",
    "data_rejeicao": "TIMESTAMP",
}


def _migrar(con: sqlite3.Connection) -> None:
    """Adiciona colunas novas a bancos pré-existentes (idempotente)."""
    existentes = {row[1] for row in con.execute("PRAGMA table_info(amostras)").fetchall()}
    for coluna, tipo in _COLUNAS_MIGRACAO.items():
        if coluna not in existentes:
            con.execute(f"ALTER TABLE amostras ADD COLUMN {coluna} {tipo}")
    con.commit()


def _reclassificar_2026(con: sqlite3.Connection) -> int:
    """Reconcilia bancos já populados com a regra de reclassificação 2026.

    As 73 amostras D (ni_ano=2026, nº 1–976) que foram importadas antes da regra
    estão como ``D{n}/25`` (ano_verdade=2025) + flag ANO_NI_DIVERGE. Esta migração:

      - move ano_verdade -> 2026 e remapeia a chave para ``D{n}/26``;
      - recalcula as flags (a divergência some, pois ni_ano passa a bater);
      - PRESERVA o progresso de reprocesso/rejeição e as datas;
      - atualiza as referências em ``eventos`` (FK por chave).

    Idempotente: só age sobre linhas que ainda não foram reclassificadas. Roda em
    transação; se a nova chave já existir (colisão), pula aquela linha por
    segurança (não há colisão nos dados atuais). Importado tardiamente para
    evitar ciclo de import com parsing.
    """
    from src.parsing import (
        calcular_flags,
        montar_chave,
        reclassificar_2026,
    )

    # Candidatas ainda não reclassificadas: ni_ano=2026, prefixo D, nº 1–976,
    # mas ano_verdade ainda != 2026.
    candidatas = con.execute(
        "SELECT chave, prefixo, numero_sequencial, ni_ano, ano_verdade, "
        "data_coleta, data_sintomas "
        "FROM amostras WHERE prefixo = 'D' AND ni_ano = 2026 "
        "AND numero_sequencial BETWEEN 1 AND 976 AND ano_verdade != 2026"
    ).fetchall()

    if not candidatas:
        return 0

    # A chave é PK referenciada por eventos(chave). Como remapeamos amostras E
    # eventos no mesmo passo, desligamos a checagem de FK durante a migração
    # (deve ser feito fora de qualquer transação) e religamos ao final.
    con.commit()  # encerra transação implícita pendente
    con.execute("PRAGMA foreign_keys = OFF")

    movidas = 0
    for r in candidatas:
        if not reclassificar_2026(r["prefixo"], r["numero_sequencial"], r["ni_ano"]):
            continue
        nova_chave = montar_chave(r["prefixo"], r["numero_sequencial"], 2026)
        # Evita colisão (não esperada nos dados atuais).
        existe = con.execute(
            "SELECT 1 FROM amostras WHERE chave = ?", (nova_chave,)
        ).fetchone()
        if existe and nova_chave != r["chave"]:
            continue
        flags = calcular_flags(
            ni_ano=r["ni_ano"],
            ano_verdade_=2026,
            data_coleta=_parse_iso(r["data_coleta"]),
            data_sintomas=_parse_iso(r["data_sintomas"]),
        )
        # Atualiza a amostra in-place (preserva progresso/rejeição/datas)...
        con.execute(
            "UPDATE amostras SET chave = ?, ano_verdade = 2026, flags = ?, "
            "atualizado_em = CURRENT_TIMESTAMP WHERE chave = ?",
            (nova_chave, flags, r["chave"]),
        )
        # ...e as referências de auditoria (FK conferida só no commit).
        con.execute(
            "UPDATE eventos SET chave = ? WHERE chave = ?",
            (nova_chave, r["chave"]),
        )
        movidas += 1

    con.commit()
    con.execute("PRAGMA foreign_keys = ON")  # religa a checagem de FK
    return movidas


def _parse_iso(valor) -> Optional["datetime"]:
    """Converte 'YYYY-MM-DD' (ou None) em datetime para recálculo de flags."""
    from datetime import datetime

    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


def criar_schema(con: sqlite3.Connection) -> None:
    """Cria tabelas e índices (idempotente — usa IF NOT EXISTS) e migra colunas."""
    con.executescript(_SCHEMA)
    _migrar(con)
    _reclassificar_2026(con)
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

# Motivos válidos de rejeição (decisão do usuário).
MOTIVOS_REJEICAO = ("Volume Insuficiente", "Não Encontrada")

# Mapa fase->cláusula WHERE. Cada amostra cai em exatamente uma (partição completa).
# Rejeitada é um estado terminal alternativo: amostras rejeitadas saem das demais
# fases (todas as fases do fluxo exigem rejeitada = 0).
FASES: dict[str, str] = {
    "pendente": "coletada = 0 AND rejeitada = 0",
    "coletada": "coletada = 1 AND extraida = 0 AND rejeitada = 0",
    "extraida": "extraida = 1 AND pcr_feito = 0 AND rejeitada = 0",
    "pcr_feito": "pcr_feito = 1 AND rejeitada = 0",
    "rejeitada": "rejeitada = 1",
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


def rejeitar(
    con: sqlite3.Connection,
    chaves: Iterable[str],
    motivo: str,
) -> int:
    """Rejeita amostras em lote (estado terminal alternativo).

    Decisão do usuário: só é possível rejeitar amostras que ainda estão
    PENDENTES (não coletadas e não já rejeitadas). Se alguma chave do lote não
    estiver pendente, recusa o lote inteiro (TransicaoInvalida, atômico). O
    motivo é obrigatório e deve ser um de MOTIVOS_REJEICAO.

    Returns:
        Número de amostras efetivamente rejeitadas.
    """
    if motivo not in MOTIVOS_REJEICAO:
        raise ValueError(f"motivo inválido: {motivo!r} (use {list(MOTIVOS_REJEICAO)})")
    chaves = list(dict.fromkeys(chaves))
    if not chaves:
        return 0

    ph = _placeholders(len(chaves))
    # Pré-requisito: todas pendentes (coletada=0 AND rejeitada=0).
    inelegiveis = con.execute(
        f"SELECT COUNT(*) FROM amostras "
        f"WHERE chave IN ({ph}) AND NOT (coletada = 0 AND rejeitada = 0)",
        chaves,
    ).fetchone()[0]
    if inelegiveis:
        raise TransicaoInvalida(
            f"{inelegiveis} amostra(s) não estão pendentes — só é possível "
            f"rejeitar amostras pendentes."
        )

    cur = con.execute(
        f"UPDATE amostras "
        f"SET rejeitada = 1, motivo_rejeicao = ?, "
        f"    data_rejeicao = CURRENT_TIMESTAMP, atualizado_em = CURRENT_TIMESTAMP "
        f"WHERE chave IN ({ph}) AND rejeitada = 0",
        [motivo, *chaves],
    )
    alteradas = cur.rowcount
    for chave in chaves:
        registrar_evento(con, chave, "rejeitada", motivo)
    con.commit()
    return alteradas


def reverter_rejeicao(con: sqlite3.Connection, chaves: Iterable[str]) -> int:
    """Desfaz a rejeição em lote: a amostra volta a Pendente.

    Limpa rejeitada/motivo/data e grava evento. Returns: nº de alteradas.
    """
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


def contagens_por_fase(con: sqlite3.Connection, where: Optional[str] = None,
                       params: Iterable = ()) -> dict[str, int]:
    """Contadores de cada fase + total, opcionalmente sobre um subconjunto filtrado.

    Combinando o WHERE do filtro com a cláusula de cada fase, os cards de métrica
    podem refletir só as amostras visíveis sob os filtros correntes (Fase 4).
    """
    params = tuple(params)
    out = {}
    for fase, clausula in FASES.items():
        w = _combinar_where(where, clausula)
        out[fase] = contar(con, where=w, params=params)
    out["total"] = contar(con, where=where, params=params)
    return out


# --------------------------------------------------------------------------- #
# Filtros da UI (Fase 4): ano, município, busca por NI, presença de flags      #
# --------------------------------------------------------------------------- #

def _combinar_where(*clausulas: Optional[str]) -> Optional[str]:
    """Junta cláusulas WHERE não-vazias com AND (cada uma entre parênteses)."""
    partes = [f"({c})" for c in clausulas if c]
    return " AND ".join(partes) if partes else None


def valores_distintos(con: sqlite3.Connection, coluna: str) -> list:
    """Valores distintos não-nulos de uma coluna, ordenados (para dropdowns).

    Só aceita colunas conhecidas (evita SQL injection via nome de coluna).
    """
    permitidas = {"ano_verdade", "municipio", "caso", "prefixo"}
    if coluna not in permitidas:
        raise ValueError(f"coluna não permitida para distinct: {coluna!r}")
    rows = con.execute(
        f"SELECT DISTINCT {coluna} FROM amostras "
        f"WHERE {coluna} IS NOT NULL AND {coluna} != '' ORDER BY {coluna}"
    ).fetchall()
    return [r[0] for r in rows]


def construir_filtro(
    *,
    ano: Optional[int] = None,
    municipio: Optional[str] = None,
    busca_ni: Optional[str] = None,
    flags_qualquer: Optional[Iterable[str]] = None,
    com_flags: Optional[bool] = None,
) -> tuple[Optional[str], list]:
    """Monta (where, params) a partir dos filtros da UI.

    Args:
        ano: filtra por ano_verdade exato.
        municipio: filtra por município exato.
        busca_ni: substring case-insensitive no NI original (ou na chave).
        flags_qualquer: lista de flags; casa amostras que tenham QUALQUER uma.
        com_flags: True = só amostras com alguma flag; False = só sem flag;
                   None = não filtra por presença.

    Returns:
        (where, params) prontos para listar_amostras/contar. where=None se vazio.
    """
    clausulas: list[str] = []
    params: list = []

    if ano is not None:
        clausulas.append("ano_verdade = ?")
        params.append(ano)

    if municipio:
        clausulas.append("municipio = ?")
        params.append(municipio)

    if busca_ni:
        # Busca no NI como veio e também na chave (cobre prefixo/ano formatado).
        clausulas.append("(ni_original LIKE ? OR chave LIKE ?)")
        termo = f"%{busca_ni.strip()}%"
        params.extend([termo, termo])

    if flags_qualquer:
        ors = []
        for f in flags_qualquer:
            ors.append("flags LIKE ?")
            params.append(f"%{f}%")
        if ors:
            clausulas.append("(" + " OR ".join(ors) + ")")

    if com_flags is True:
        clausulas.append("flags != ''")
    elif com_flags is False:
        clausulas.append("flags = ''")

    where = " AND ".join(clausulas) if clausulas else None
    return where, params
