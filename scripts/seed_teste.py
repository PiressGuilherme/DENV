"""Popula um banco de TESTE com dados realistas para experimentar o fluxo.

Importa a planilha de origem e faz um subconjunto avançar até 'PCR feito' (e uma
parte até 'Sequenciado'), de modo que a aba PCR feito já tenha o que importar e
a aba Sequenciadas já tenha o que mostrar.

RECUSA-SE a rodar contra um banco que já tenha amostras com progresso marcado,
para não haver como apontar sem querer para a produção.

Uso:
    DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/denv_test" \
        python -m scripts.seed_teste
"""

from __future__ import annotations

import argparse
import sys

from src import db

# Quantas amostras levar até cada etapa (as primeiras na ordenação canônica).
PADRAO_ATE_PCR = 120
PADRAO_ATE_SEQUENCIADO = 25


def _guarda_producao(con) -> None:
    """Aborta se o banco parece ser o de produção (já tem progresso marcado)."""
    marcadas = db.contar(con, "coletada = 1 OR rejeitada = 1")
    if marcadas:
        raise SystemExit(
            f"ABORTADO: este banco já tem {marcadas} amostra(s) com progresso "
            "marcado — parece ser produção. O seed só roda em banco limpo.\n"
            "Confira o DATABASE_URL apontado."
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ate-pcr", type=int, default=PADRAO_ATE_PCR)
    parser.add_argument("--ate-sequenciado", type=int, default=PADRAO_ATE_SEQUENCIADO)
    args = parser.parse_args(argv[1:])

    if not db._DATABASE_URL:
        print("ERRO: DATABASE_URL não definido.", file=sys.stderr)
        return 2
    if args.ate_sequenciado > args.ate_pcr:
        print("ERRO: --ate-sequenciado não pode exceder --ate-pcr.", file=sys.stderr)
        return 2

    con = db.init_db()
    try:
        _guarda_producao(con)

        if db.contar(con) == 0:
            print("Banco vazio — importando a planilha de origem...")
            from src.importer import importar
            r = importar(verificar_sanidade=False, _con=con)
            print(f"  {r.amostras_unicas} amostras importadas.")

        chaves = [r["chave"] for r in db.listar_amostras(con)][:args.ate_pcr]
        if not chaves:
            print("ERRO: nenhuma amostra no banco.", file=sys.stderr)
            return 1

        for etapa in ("coletada", "extraida", "pcr_feito"):
            n = db.avancar_fase(con, chaves, etapa)
            print(f"  {n} -> {etapa}")

        seq = chaves[:args.ate_sequenciado]
        print(f"  {db.avancar_fase(con, seq, 'sequenciado')} -> sequenciado")

        cont = db.contagens_por_fase(con)
        print("\nContagens por fase:")
        for fase in db.FASES:
            print(f"  {db.LABEL_FASE[fase]:<14} {cont[fase]:>5}")
        print(f"  {'TOTAL':<14} {cont['total']:>5}")
        print(
            "\nObs.: Sequenciadas é subconjunto de PCR feito — os cards não somam "
            "o total, por design."
        )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
