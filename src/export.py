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
    ("coletada", "Coletada"),
    ("data_coletada", "Data Coletada"),
    ("extraida", "Extraída"),
    ("data_extraida", "Data Extraída"),
    ("pcr_feito", "PCR feito"),
    ("data_pcr", "Data PCR"),
    ("rejeitada", "Rejeitada"),
    ("motivo_rejeicao", "Motivo Rejeição"),
    ("data_rejeicao", "Data Rejeição"),
    ("flags", "Flags"),
    ("n_origem", "Nº origem"),
)

_LABEL_FASE = {
    "pendente": "Pendente",
    "coletada": "Coletada",
    "extraida": "Extraída",
    "pcr_feito": "PCR feito",
    "rejeitada": "Rejeitada",
}


def _fase_da_linha(r) -> str:
    """Deriva o rótulo da fase (espelha db.FASES; partição completa)."""
    if r["rejeitada"]:
        return _LABEL_FASE["rejeitada"]
    if r["pcr_feito"]:
        return _LABEL_FASE["pcr_feito"]
    if r["extraida"]:
        return _LABEL_FASE["extraida"]
    if r["coletada"]:
        return _LABEL_FASE["coletada"]
    return _LABEL_FASE["pendente"]


def _valor(r, campo: str):
    """Extrai o valor de uma linha para o export, normalizando booleanos/fase."""
    if campo == "fase":
        return _fase_da_linha(r)
    val = r[campo]
    # Campos 0/1 viram Sim/Não para leitura humana na bancada.
    if campo in ("coletada", "extraida", "pcr_feito", "rejeitada"):
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
