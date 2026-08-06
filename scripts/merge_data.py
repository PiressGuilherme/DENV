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
from openpyxl import load_workbook

from src.parsing import ano_verdade, montar_chave, parse_ni

DATA = Path(__file__).resolve().parent.parent / "data"
ARQUIVOS = ("data1.csv", "data2.csv", "data3.csv")
SAIDA = DATA / "dengue_consolidado.xlsx"

COL_NI = "Número Interno"
COL_SINTOMAS = "Data do 1º Sintomas"
COL_COLETA = "Data da Coleta"

# Prioridade de exame ao escolher a linha sobrevivente de cada amostra.
# A PCR (ZDC) vem primeiro: é o exame que o sistema de reprocesso acompanha,
# e é dele que saem os Ct do termociclador.
_PRIORIDADE_EXAME = {
    "Pesquisa de Arbovírus (ZDC)": 0,
    "Dengue, Detecção de Antígeno NS1": 1,
    "Dengue, IgM": 2,
}

# Desempate dentro do mesmo exame: uma linha com resultado liberado descreve
# melhor a amostra do que uma cancelada.
_PRIORIDADE_STATUS = {
    "Resultado Liberado": 0,
    "Resultado Cadastrado": 1,
    "Exame em Análise": 2,
    "Aguardando Triagem": 3,
    "Exame não-realizado": 4,
    "Exame Cancelado": 5,
}


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
    """Reduz cada amostra a UMA linha, preservando o layout original do GAL.

    Não cria colunas novas: escolhe a linha mais representativa da amostra e
    descarta as demais. A prioridade é a linha de PCR (ZDC) e, dentro dela, a de
    status mais avançado — é essa que o sistema de reprocesso consome.
    """
    df = df.copy()
    df["_p_exame"] = df["Exame"].map(_PRIORIDADE_EXAME).fillna(99).astype(int)
    df["_p_status"] = df["Status Exame"].map(_PRIORIDADE_STATUS).fillna(99).astype(int)

    escolhidas = (
        df.sort_values(["_p_exame", "_p_status"], kind="stable")
        .groupby("_ni", sort=False, as_index=False)
        .head(1)
    )
    return escolhidas.drop(columns=["_p_exame", "_p_status"])


def _escrever(df: pd.DataFrame, destino: Path) -> None:
    """Grava o xlsx com formatação legível.

    O default do openpyxl é Calibri 11 sem larguras definidas, o que deixa a
    planilha com aparência de texto cru. Aqui o corpo usa Arial (mesma fonte da
    planilha de origem do projeto), o cabeçalho fica em negrito congelado e as
    colunas ganham largura proporcional ao conteúdo.
    """
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    df.to_excel(destino, index=False, engine="openpyxl")

    wb = load_workbook(destino)
    ws = wb.active

    corpo = Font(name="Arial", size=10)
    cabecalho = Font(name="Arial", size=10, bold=True)

    for celula in ws[1]:
        celula.font = cabecalho
        celula.alignment = Alignment(horizontal="center", vertical="center")

    for linha in ws.iter_rows(min_row=2):
        for celula in linha:
            celula.font = corpo

    # Largura pelo maior conteúdo real da coluna, com teto para não estourar a
    # tela por causa de campos livres como Observação.
    for i, nome in enumerate(df.columns, start=1):
        maior = int(df[nome].astype(str).str.len().max() or 0)
        largura = max(len(str(nome)), min(maior, 40)) + 2
        ws.column_dimensions[get_column_letter(i)].width = largura

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(destino)


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
    n_pcr = (final["Exame"] == "Pesquisa de Arbovírus (ZDC)").sum()
    print(f"[4] Consolidado: {len(final)} amostras únicas ({n_pcr} com linha de PCR)")

    # Ordenação canônica da Seção 3.3: ano_verdade, prefixo, numero_sequencial.
    # numero_sequencial precisa ser INTEGER (3.3) — como texto, D1000 viria
    # antes de D999.
    final["_ano"] = final["_ano"].astype(int)
    final["_numero"] = final["_numero"].astype(int)
    final = final.sort_values(
        ["_ano", "_prefixo", "_numero"], kind="stable"
    ).reset_index(drop=True)

    # Volta ao layout original do GAL: só as colunas do export, sem auxiliares.
    final = final[[c for c in final.columns if not c.startswith("_")]]

    # Datas de sintomas e coleta lado a lado, logo após o Número Interno.
    cols = [c for c in final.columns if c not in (COL_SINTOMAS, COL_COLETA)]
    i = cols.index(COL_NI) + 1
    cols[i:i] = [COL_SINTOMAS, COL_COLETA]
    final = final[cols]

    _escrever(final, SAIDA)
    print(f"[5] Planilha gerada: {SAIDA}")
    print(f"    {len(final)} linhas x {len(final.columns)} colunas")


if __name__ == "__main__":
    main()
