"""Importador xlsx -> dedup -> SQLite (Seção 7 do CLAUDE.md).

Comportamento exigido:
    1. Ler o xlsx, parsear datas explicitamente.
    2. Descartar linhas sem NI / NI inválido (log da contagem ~1.276).
    3. Parsear NI (via parsing.parse_ni).
    4. Calcular ano_verdade (Data da Coleta > NI).
    5. Agrupar por chave; campos descritivos vêm da linha mais recente por
       Data da Coleta dentro do grupo.
    6. Calcular n_origem (tamanho do grupo) e flags.
    7. IDEMPOTENTE: reimportar NÃO zera o progresso de reprocesso já marcado.
       UPSERT atualiza só descritivos/flags e preserva coletada/extraida/pcr_feito
       e suas datas.
    8. Asserts de sanidade: total ≈ 5.506; 2025 ≈ 3.488; 2026 ≈ 2.018.

Uso CLI:
    python -m src.importer [caminho_xlsx] [caminho_db]
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from src import db
from src.parsing import (
    ano_verdade,
    calcular_flags,
    montar_chave,
    parse_ni,
)

# Caminhos default (Seção 5).
RAIZ = Path(__file__).resolve().parent.parent
XLSX_PADRAO = RAIZ / "data" / "dengue_coleta_dentro_prazo_mun_ordenado.xlsx"
ABA = "dengue_coleta_dentro_prazo_mun_"

# Nomes das colunas na planilha de origem.
COL_NI = "Número Interno"
COL_REQUISICAO = "Requisição"
COL_MUNICIPIO = "Municipio de Residência"
COL_DATA_COLETA = "Data da Coleta"
COL_DATA_SINTOMAS = "Data do 1º Sintomas"
COL_CASO = "Caso"


@dataclass
class ResultadoImport:
    """Sumário do import, para log e asserts de sanidade."""

    linhas_brutas: int = 0
    ignoradas_sem_ni: int = 0
    ignoradas_ni_invalido: int = 0
    amostras_unicas: int = 0
    por_ano: dict[int, int] = field(default_factory=dict)
    inseridas: int = 0
    atualizadas: int = 0

    @property
    def total_ignoradas(self) -> int:
        return self.ignoradas_sem_ni + self.ignoradas_ni_invalido


def _como_data(valor) -> Optional[datetime]:
    """Converte célula da planilha em datetime, tolerando NaT/NaN/None."""
    if valor is None:
        return None
    if isinstance(valor, float) and pd.isna(valor):
        return None
    if pd.isna(valor):
        return None
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, date):
        return datetime(valor.year, valor.month, valor.day)
    # pandas Timestamp ou string -> tentar converter
    ts = pd.to_datetime(valor, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def _texto(valor) -> Optional[str]:
    """Normaliza célula em texto, tolerando NaN/None."""
    if valor is None:
        return None
    if isinstance(valor, float) and pd.isna(valor):
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    s = str(valor).strip()
    return s or None


def _iso(d: Optional[datetime]) -> Optional[str]:
    """datetime -> 'YYYY-MM-DD' (ou None)."""
    return d.date().isoformat() if d is not None else None


@dataclass
class _Linha:
    """Linha bruta já parseada e pronta para agrupamento."""

    chave: str
    prefixo: str
    numero_sequencial: int
    ano_verdade: int
    ni_original: str
    ni_ano: int
    requisicao: Optional[str]
    municipio: Optional[str]
    data_coleta: Optional[datetime]
    data_sintomas: Optional[datetime]
    caso: Optional[str]


def _ler_e_parsear(xlsx: Path, resultado: ResultadoImport) -> list[_Linha]:
    """Lê o xlsx e devolve linhas válidas já parseadas (passos 1-4)."""
    df = pd.read_excel(xlsx, sheet_name=ABA, dtype=object)
    resultado.linhas_brutas = len(df)

    linhas: list[_Linha] = []
    for row in df.itertuples(index=False):
        d = dict(zip(df.columns, row))
        ni_raw = d.get(COL_NI)
        ni_texto = _texto(ni_raw)
        if ni_texto is None:
            resultado.ignoradas_sem_ni += 1
            continue
        p = parse_ni(ni_texto)
        if p is None:
            resultado.ignoradas_ni_invalido += 1
            continue

        data_coleta = _como_data(d.get(COL_DATA_COLETA))
        av = ano_verdade(p.ni_ano, data_coleta)
        if av is None:
            # Sem ano-de-verdade não há como posicionar a amostra; descarta.
            resultado.ignoradas_ni_invalido += 1
            continue

        chave = montar_chave(p.prefixo, p.numero_sequencial, av)
        linhas.append(
            _Linha(
                chave=chave,
                prefixo=p.prefixo,
                numero_sequencial=p.numero_sequencial,
                ano_verdade=av,
                ni_original=ni_texto,
                ni_ano=p.ni_ano,
                requisicao=_texto(d.get(COL_REQUISICAO)),
                municipio=_texto(d.get(COL_MUNICIPIO)),
                data_coleta=data_coleta,
                data_sintomas=_como_data(d.get(COL_DATA_SINTOMAS)),
                caso=_texto(d.get(COL_CASO)),
            )
        )
    return linhas


def _agrupar(linhas: list[_Linha]) -> dict[str, dict]:
    """Agrupa por chave (passos 5-6).

    Campos descritivos vêm da linha mais recente por Data da Coleta dentro do
    grupo. n_origem = tamanho do grupo. flags recalculadas com a linha escolhida.
    """
    grupos: dict[str, list[_Linha]] = {}
    for l in linhas:
        grupos.setdefault(l.chave, []).append(l)

    amostras: dict[str, dict] = {}
    for chave, grupo in grupos.items():
        # Mais recente por Data da Coleta; None vai para o fim (menos recente).
        rep = max(
            grupo,
            key=lambda x: (x.data_coleta is not None, x.data_coleta or datetime.min),
        )
        flags = calcular_flags(
            ni_ano=rep.ni_ano,
            ano_verdade_=rep.ano_verdade,
            data_coleta=rep.data_coleta,
            data_sintomas=rep.data_sintomas,
        )
        amostras[chave] = {
            "chave": chave,
            "prefixo": rep.prefixo,
            "numero_sequencial": rep.numero_sequencial,
            "ano_verdade": rep.ano_verdade,
            "ni_original": rep.ni_original,
            "ni_ano": rep.ni_ano,
            "requisicao": rep.requisicao,
            "municipio": rep.municipio,
            "data_coleta": _iso(rep.data_coleta),
            "data_sintomas": _iso(rep.data_sintomas),
            "caso": rep.caso,
            "n_origem": len(grupo),
            "flags": flags,
        }
    return amostras


# UPSERT idempotente: insere se novo; em conflito de chave, atualiza SÓ os campos
# descritivos/flags e preserva os campos de reprocesso (Seção 7, passo 7).
_UPSERT = """
INSERT INTO amostras (
    chave, prefixo, numero_sequencial, ano_verdade, ni_original, ni_ano,
    requisicao, municipio, data_coleta, data_sintomas, caso, n_origem, flags,
    atualizado_em
) VALUES (
    :chave, :prefixo, :numero_sequencial, :ano_verdade, :ni_original, :ni_ano,
    :requisicao, :municipio, :data_coleta, :data_sintomas, :caso, :n_origem, :flags,
    CURRENT_TIMESTAMP
)
ON CONFLICT(chave) DO UPDATE SET
    prefixo           = excluded.prefixo,
    numero_sequencial = excluded.numero_sequencial,
    ano_verdade       = excluded.ano_verdade,
    ni_original       = excluded.ni_original,
    ni_ano            = excluded.ni_ano,
    requisicao        = excluded.requisicao,
    municipio         = excluded.municipio,
    data_coleta       = excluded.data_coleta,
    data_sintomas     = excluded.data_sintomas,
    caso              = excluded.caso,
    n_origem          = excluded.n_origem,
    flags             = excluded.flags,
    atualizado_em     = CURRENT_TIMESTAMP
;
"""


def _persistir(con: sqlite3.Connection, amostras: dict[str, dict], resultado: ResultadoImport) -> None:
    """Faz o UPSERT idempotente de cada amostra (passo 7)."""
    existentes = {r[0] for r in con.execute("SELECT chave FROM amostras").fetchall()}
    for chave, dados in amostras.items():
        con.execute(_UPSERT, dados)
        if chave in existentes:
            resultado.atualizadas += 1
        else:
            resultado.inseridas += 1
    con.commit()


def importar(
    xlsx: Path | str = XLSX_PADRAO,
    db_path: Path | str = db.DB_PATH,
    *,
    verificar_sanidade: bool = True,
) -> ResultadoImport:
    """Executa o import completo (idempotente) e retorna o sumário.

    Args:
        xlsx: caminho da planilha de origem (read-only).
        db_path: caminho do SQLite (criado se não existir).
        verificar_sanidade: se True, assert nas contagens da Seção 7 passo 8.
    """
    xlsx = Path(xlsx)
    resultado = ResultadoImport()

    linhas = _ler_e_parsear(xlsx, resultado)
    amostras = _agrupar(linhas)
    resultado.amostras_unicas = len(amostras)
    resultado.por_ano = {}
    for dados in amostras.values():
        a = dados["ano_verdade"]
        resultado.por_ano[a] = resultado.por_ano.get(a, 0) + 1

    con = db.init_db(db_path)
    try:
        _persistir(con, amostras, resultado)
    finally:
        con.close()

    if verificar_sanidade:
        _assert_sanidade(resultado)

    return resultado


def _assert_sanidade(r: ResultadoImport) -> None:
    """Asserts de sanidade da Seção 7 passo 8 (com tolerância pequena)."""
    assert r.total_ignoradas == 1276, f"ignoradas={r.total_ignoradas} (esperado 1276)"
    assert r.amostras_unicas == 5506, f"únicas={r.amostras_unicas} (esperado 5506)"
    assert r.por_ano.get(2025) == 3488, f"2025={r.por_ano.get(2025)} (esperado 3488)"
    assert r.por_ano.get(2026) == 2018, f"2026={r.por_ano.get(2026)} (esperado 2018)"


def main(argv: list[str]) -> int:
    xlsx = Path(argv[1]) if len(argv) > 1 else XLSX_PADRAO
    db_path = Path(argv[2]) if len(argv) > 2 else db.DB_PATH
    r = importar(xlsx, db_path, verificar_sanidade=False)
    print(f"Linhas brutas      : {r.linhas_brutas}")
    print(f"Ignoradas sem NI   : {r.ignoradas_sem_ni}")
    print(f"Ignoradas NI inval.: {r.ignoradas_ni_invalido}")
    print(f"Total ignoradas    : {r.total_ignoradas}")
    print(f"Amostras únicas    : {r.amostras_unicas}")
    for ano in sorted(r.por_ano):
        print(f"  {ano}: {r.por_ano[ano]}")
    print(f"Inseridas          : {r.inseridas}")
    print(f"Atualizadas        : {r.atualizadas}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
