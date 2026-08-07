"""Remove do banco as amostras cuja PCR já foi concluída em ciclo anterior.

O sistema conta a história do que a equipe ATUAL fez e precisa fazer. Amostras
cuja PCR já foi concluída e liberada no GAL pertencem a um ciclo anterior: elas
não entram no fluxo de reprocesso e só poluiriam as abas de trabalho.

SEGURANÇA — uma amostra só é removida se, nas planilhas do GAL, ela tiver:

    1. linha de exame "Pesquisa de Arbovírus (ZDC)" (a PCR), E
    2. status concluído (Resultado Liberado/Cadastrado) OU data de processamento

O Kit sozinho NÃO é prova: há amostras com kit de PCR alocado cujo exame foi
cancelado sem produzir resultado — essas ainda precisam de PCR e permanecem.

Amostra sem evidência de PCR no GAL nunca é tocada: a lista de exclusão é
derivada da planilha, não do banco, e cada chave é reconferida antes do DELETE.

Uso:
    python -m scripts.remover_pcr_anterior            # dry-run (não altera nada)
    python -m scripts.remover_pcr_anterior --executar # aplica
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from scripts.merge_data import ABA, COL_NI, ja_fez_pcr
from src import db

DATA = Path(__file__).resolve().parent.parent / "data"
PLANILHA_TOTAL = DATA / "dengue_consolidado.xlsx"


def chaves_com_pcr_concluida(planilha: Path) -> tuple[set[str], pd.DataFrame]:
    """Chaves cuja PCR foi concluída, segundo as planilhas do GAL.

    Returns:
        (conjunto de chaves, DataFrame com a evidência de cada uma).
    """
    df = pd.read_excel(planilha, sheet_name=ABA, dtype=object)
    fez = df.apply(ja_fez_pcr, axis=1)
    evidencia = df[fez]
    return set(evidencia[COL_NI].astype(str)), evidencia


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--executar", action="store_true",
                    help="aplica as remoções (sem esta flag, apenas simula)")
    ap.add_argument("--planilha", type=Path, default=PLANILHA_TOTAL)
    args = ap.parse_args()

    chaves, evidencia = chaves_com_pcr_concluida(args.planilha)
    print(f"[1] Planilha: {args.planilha.name}")
    print(f"    {len(chaves)} amostras com PCR concluída no GAL")

    con = db.init_db()
    try:
        existentes = {
            r["chave"] for r in con.execute("SELECT chave FROM amostras").fetchall()
        }
        alvo = sorted(chaves & existentes)
        print(f"[2] Dessas, {len(alvo)} estão no banco (as demais nunca entraram)")

        if not alvo:
            print("Nada a remover.")
            return 0

        # Trava de segurança: reconfere a evidência de CADA chave antes do DELETE.
        # Se alguma não tiver a prova esperada, aborta tudo em vez de remover
        # parcialmente.
        por_chave = evidencia.set_index(evidencia[COL_NI].astype(str))
        sem_prova = [k for k in alvo if k not in por_chave.index]
        if sem_prova:
            print(f"ABORTADO: {len(sem_prova)} chaves sem evidência: {sem_prova[:5]}")
            return 1

        # Mostra o estado de bancada do que será removido — se houver progresso
        # registrado, o operador precisa saber antes de confirmar.
        marcadores = ("coletada", "extraida", "pcr_feito", "sequenciado", "rejeitada")
        ph = ",".join(["%s"] * len(alvo))
        r = con.execute(
            f"SELECT COUNT(*) AS n, {', '.join(f'SUM({c}) AS {c}' for c in marcadores)}, "
            f"COUNT(data_resultado) AS com_ct "
            f"FROM amostras WHERE chave IN ({ph})",
            tuple(alvo),
        ).fetchone()
        print(f"[3] Estado de bancada das {r['n']} amostras a remover:")
        for c in marcadores:
            print(f"      {c:12} {r[c] or 0}")
        print(f"      {'com Ct':12} {r['com_ct']}")

        print("\n[4] Amostras (primeiras 10):")
        for k in alvo[:10]:
            linha = por_chave.loc[k]
            if isinstance(linha, pd.DataFrame):
                linha = linha.iloc[0]
            print(f"      {k:12} {linha['Status Exame']:22} "
                  f"proc={linha['Data do Processamento']}")

        if not args.executar:
            print(f"\nDRY-RUN: {len(alvo)} seriam removidas. "
                  f"Rode com --executar para aplicar.")
            return 0

        # eventos.chave tem FK sem ON DELETE, então o histórico sai primeiro.
        n_ev = con.execute(
            f"SELECT COUNT(*) AS n FROM eventos WHERE chave IN ({ph})", tuple(alvo)
        ).fetchone()["n"]
        con.execute(f"DELETE FROM eventos WHERE chave IN ({ph})", tuple(alvo))
        con.execute(f"DELETE FROM amostras WHERE chave IN ({ph})", tuple(alvo))
        con.commit()

        restantes = con.execute("SELECT COUNT(*) AS n FROM amostras").fetchone()["n"]
        print(f"\n[5] Removidas {len(alvo)} amostras e {n_ev} eventos.")
        print(f"    Total no banco agora: {restantes}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
