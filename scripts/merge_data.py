"""Consolida data1/data2/data3 (exports do GAL) numa planilha única de dengue.

Os três CSVs compartilham o mesmo schema do GAL (110 colunas, `;`, ISO-8859-1),
mas NÃO são fatias disjuntas: data1∩data3 e data2∩data3 se sobrepõem, e a
granularidade é uma linha por EXAME, não por amostra (NS1, IgM e ZDC viram três
linhas do mesmo Número Interno).

Este script empilha os três, mantém só amostras de dengue (NI começando com D) e
colapsa para UMA linha por Número Interno. Os campos de identificação (paciente,
município, datas de coleta/sintomas) são constantes dentro de uma amostra, então
podem vir de qualquer linha; já os campos de resultado variam por exame — por
isso cada exame é preservado em colunas próprias (ver _COLUNAS_EXAME) em vez de
escolher uma linha e descartar as demais, o que perderia o resultado da PCR.

Uso:
    python -m scripts.merge_data
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from src.parsing import ano_verdade, calcular_flags, montar_chave, parse_ni

DATA = Path(__file__).resolve().parent.parent / "data"
ARQUIVOS = ("data1.csv", "data2.csv", "data3.csv")
SAIDA = DATA / "dengue_consolidado.xlsx"

COL_NI = "Número Interno"
COL_SINTOMAS = "Data do 1º Sintomas"
COL_COLETA = "Data da Coleta"

# Exames presentes no GAL -> sufixo curto usado nas colunas de resultado.
_EXAMES = {
    "Pesquisa de Arbovírus (ZDC)": "ZDC",
    "Dengue, Detecção de Antígeno NS1": "NS1",
    "Dengue, IgM": "IgM",
}

# Colunas cujo valor depende do exame — replicadas por tipo de exame.
_COLUNAS_EXAME = (
    "Metodologia",
    "Status Exame",
    "Data do Processamento",
    "Data da Liberação",
    "1º Campo Resultado",
    "2º Campo Resultado",
    "3º Campo Resultado",
    "4º Campo Resultado",
    "5º Campo Resultado",
    "6º Campo Resultado",
)


def _ler(caminho: Path) -> pd.DataFrame:
    """Lê um export do GAL preservando tudo como texto.

    dtype=str + keep_default_na=False evitam que o pandas transforme códigos em
    float (431490.0) ou vazios em NaN — ambos corromperiam identificadores.
    """
    df = pd.read_csv(
        caminho, sep=";", encoding="iso-8859-1", dtype=str, keep_default_na=False
    )
    df["_origem"] = caminho.stem
    return df


def _data(valor: str):
    """Converte as datas do GAL (dd/mm/aa ou dd-mm-aaaa) em datetime."""
    texto = str(valor).strip()
    if not texto:
        return None
    for fmt in ("%d/%m/%y", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    return None


def resolver_ni(ni_bruto: str, data_coleta_bruta: str) -> Optional[dict]:
    """Aplica as regras da Seção 3 da ESPECIFICACAO ao NI de uma linha.

    Delega inteiramente a src.parsing — a mesma lógica que o importador usa —
    em vez de reimplementar o parse aqui. Isso traz de graça a normalização de
    caixa ('d3520/26'), o ano com zero à esquerda ('D1612/026'), o ano-de-verdade
    vindo da Data da Coleta (3.1) e a reclassificação 2026 (3.5).

    Returns:
        dict com chave/prefixo/numero/ano_verdade/flags, ou None se o NI não for
        parseável (linha descartada, como no passo 2 do importador).
    """
    p = parse_ni(ni_bruto)
    if p is None:
        return None

    coleta = _data(data_coleta_bruta)
    ano = ano_verdade(
        p.ni_ano,
        coleta,
        prefixo=p.prefixo,
        numero_sequencial=p.numero_sequencial,
    )
    if ano is None:
        return None

    return {
        "chave": montar_chave(p.prefixo, p.numero_sequencial, ano),
        "prefixo": p.prefixo,
        "numero": p.numero_sequencial,
        "ni_ano": p.ni_ano,
        "ano_verdade": ano,
        "coleta": coleta,
    }


def consolidar(df: pd.DataFrame) -> pd.DataFrame:
    """Colapsa as linhas de exame de cada amostra numa única linha."""
    # Campos estáveis dentro da amostra: primeira ocorrência não-vazia.
    fixas = [c for c in df.columns
             if c not in _COLUNAS_EXAME and c not in ("Exame", "_origem", "_ni")]
    # _prefixo/_numero/_ano são constantes na amostra e seguem para a ordenação.

    def _primeiro_nao_vazio(s: pd.Series) -> str:
        for v in s:
            if str(v).strip():
                return v
        return ""

    base = df.groupby("_ni", sort=False)[fixas].agg(_primeiro_nao_vazio)

    # Campos de resultado: uma coluna por (exame, campo).
    blocos = [base]
    for exame, sufixo in _EXAMES.items():
        sub = df[df["Exame"] == exame]
        if sub.empty:
            continue
        agg = sub.groupby("_ni", sort=False)[list(_COLUNAS_EXAME)].agg(
            _primeiro_nao_vazio
        )
        agg.columns = [f"{c} [{sufixo}]" for c in agg.columns]
        blocos.append(agg)

    # Rastreabilidade: de quais arquivos e exames a amostra veio.
    meta = df.groupby("_ni", sort=False).agg(
        Exames_Encontrados=("Exame", lambda s: " | ".join(sorted(set(s)))),
        Arquivos_Origem=("_origem", lambda s: " | ".join(sorted(set(s)))),
        Linhas_Originais=("Exame", "size"),
    )
    blocos.append(meta)

    return pd.concat(blocos, axis=1).reset_index(drop=True)


def main() -> None:
    df = pd.concat([_ler(DATA / a) for a in ARQUIVOS], ignore_index=True)
    print(f"[1] Concatenado: {len(df)} linhas de {len(ARQUIVOS)} arquivos")

    resolvidos = [
        resolver_ni(ni, coleta)
        for ni, coleta in zip(df[COL_NI], df[COL_COLETA])
    ]
    df["_ni"] = [r["chave"] if r else "" for r in resolvidos]
    df["_prefixo"] = [r["prefixo"] if r else "" for r in resolvidos]
    df["_numero"] = [r["numero"] if r else 0 for r in resolvidos]
    df["_ano"] = [r["ano_verdade"] if r else 0 for r in resolvidos]

    # Filtro de dengue: prefixo D já normalizado em maiúsculas pelo parse_ni.
    df = df[df["_prefixo"] == "D"].copy()
    df[COL_NI] = df["_ni"]
    print(f"[2] Só amostras de dengue (prefixo D): {len(df)} linhas")

    antes = len(df)
    df = df.drop_duplicates(
        subset=[c for c in df.columns if c not in ("_origem",)]
    )
    print(f"[3] Removidas {antes - len(df)} linhas idênticas (sobreposição)")

    final = consolidar(df)
    print(f"[4] Consolidado: {len(final)} amostras únicas")

    # Flags de inconsistência (Seção 3.4) — sinalizam, nunca bloqueiam.
    final["Flags"] = [
        calcular_flags(
            ni_ano=parse_ni(ni).ni_ano if parse_ni(ni) else None,
            ano_verdade_=ano,
            data_coleta=_data(dc),
            data_sintomas=_data(ds),
        )
        for ni, ano, dc, ds in zip(
            final[COL_NI], final["_ano"], final[COL_COLETA], final[COL_SINTOMAS]
        )
    ]

    # Datas de sintomas e coleta lado a lado, logo após o Número Interno.
    cols = [c for c in final.columns if c not in (COL_SINTOMAS, COL_COLETA)]
    i = cols.index(COL_NI) + 1
    cols[i:i] = [COL_SINTOMAS, COL_COLETA]
    final = final[cols]

    # Ordenação canônica da Seção 3.3: ano_verdade, prefixo, numero_sequencial.
    # numero_sequencial precisa ser INTEGER (3.3) — como texto, D1000 viria
    # antes de D999.
    final["_ano"] = final["_ano"].astype(int)
    final["_numero"] = final["_numero"].astype(int)
    final = final.sort_values(
        ["_ano", "_prefixo", "_numero"], kind="stable"
    ).reset_index(drop=True)

    n_flags = (final["Flags"] != "").sum()
    final = final.drop(columns=["_ano", "_prefixo", "_numero"])

    final.to_excel(SAIDA, index=False, engine="openpyxl")
    print(f"[5] Planilha gerada: {SAIDA}")
    print(f"    {len(final)} linhas x {len(final.columns)} colunas")
    print(f"    {n_flags} amostras com flags de inconsistência")


if __name__ == "__main__":
    main()
