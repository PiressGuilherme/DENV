"""Export da visão atual (Seção 6.4 da ESPECIFICACAO.md).

Gera xlsx/csv da visão corrente — respeitando filtros e a ordenação canônica —
SEM reintroduzir as colunas de resultado antigo (Metodologia/Status/Campos). Só
o que importa ao reprocesso.

A montagem é pura (recebe linhas já filtradas/ordenadas e devolve bytes), logo é
testável sem subir a UI. A camada de UI (app.py) só liga o botão ao download.
"""

from __future__ import annotations

import io
from typing import Iterable, Sequence

import pandas as pd

from src import db, resultados

# Etapas do fluxo, cada uma com sua coluna de data — derivado de db.ETAPAS_DEF
# para que uma etapa nova entre no export sem editar esta lista.
_COLUNAS_ETAPAS: tuple[tuple[str, str], ...] = tuple(
    par
    for etapa in db.ETAPAS_DEF
    for par in (
        (etapa.chave, etapa.label),
        (etapa.coluna_data, etapa.label_data),
    )
)

# Ct por sorotipo, também derivado (db.SOROTIPOS).
_COLUNAS_CT: tuple[tuple[str, str], ...] = tuple(
    (db.coluna_ct(s), f"{s} (Ct)") for s in db.SOROTIPOS
)

# Colunas exportadas, na ordem de exibição. (campo_no_banco, cabeçalho PT-BR).
# Apenas campos do reprocesso + contexto mínimo — nunca as colunas antigas.
COLUNAS_EXPORT: Sequence[tuple[str, str]] = (
    ("ni_original", "NI"),
    ("numero_sequencial", "Número"),
    ("ano_verdade", "Ano"),
    ("municipio", "Município"),
    ("data_coleta", "Data Coleta"),
    ("data_sintomas", "Data 1º Sintoma"),
    ("caso", "Caso"),
    ("fase", "Fase"),
    *_COLUNAS_ETAPAS,
    ("rejeitada", "Rejeitada"),
    ("motivo_rejeicao", "Motivo Rejeição"),
    ("data_rejeicao", "Data Rejeição"),
    ("sorotipo", "Sorotipo"),
    *_COLUNAS_CT,
    ("data_resultado", "Data Resultado"),
    ("flags", "Flags"),
    ("n_origem", "Nº origem"),
)

# Campos 0/1 que viram Sim/Não para leitura humana na bancada.
_CAMPOS_BOOLEANOS = frozenset({*(e.chave for e in db.ETAPAS_DEF), "rejeitada"})


def _valor(r, campo: str):
    """Extrai o valor de uma linha para o export, normalizando booleanos/derivados."""
    if campo == "fase":
        return db.LABEL_FASE[db.fase_da_linha(r)]
    if campo == "sorotipo":
        return resultados.sorotipo_de(r)
    val = r[campo]
    if campo in _CAMPOS_BOOLEANOS:
        return "Sim" if val else "Não"
    return val


def montar_dataframe(rows: Iterable) -> pd.DataFrame:
    """Constrói o DataFrame do export a partir das linhas (dict-like rows)."""
    registros = []
    for r in rows:
        registros.append({cab: _valor(r, campo) for campo, cab in COLUNAS_EXPORT})
    cabecalhos = [cab for _, cab in COLUNAS_EXPORT]
    return pd.DataFrame(registros, columns=cabecalhos)


def para_xlsx_bytes(rows: Iterable, *, sheet_name: str = "reprocesso") -> bytes:
    """Serializa a visão em xlsx (bytes)."""
    df = montar_dataframe(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31] or "reprocesso")
    return buf.getvalue()


def para_csv_bytes(rows: Iterable) -> bytes:
    """Serializa a visão em csv (bytes, UTF-8 com BOM para abrir bem no Excel)."""
    df = montar_dataframe(rows)
    return df.to_csv(index=False).encode("utf-8-sig")
