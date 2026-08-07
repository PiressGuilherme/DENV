"""Remove do banco amostras coletadas fora da janela de sintomas.

Acima de MAX_DIAS_SINTOMA_COLETA dias entre o 1º sintoma e a coleta, a carga
viral já caiu e a PCR perde sensibilidade — a amostra não serve para reprocesso.
É a mesma regra da planilha histórica "dengue_coleta_dentro_prazo_mun_ordenado",
cuja coluna Dif Dias tem máximo 5.

Este script é o tratamento RETROATIVO: a carga de 07/08/2026 entrou antes de a
regra existir no merge_data. Novas importações já saem filtradas na origem, então
em regime normal ele não encontra nada para remover.

O cálculo é feito em SQL sobre colunas DATE nativas (data_coleta - data_sintomas),
sem parse de texto — não há ambiguidade dd/mm vs mm/dd aqui.

Diferença NEGATIVA (coleta antes do sintoma) NÃO é removida: é erro de digitação,
não amostra tardia, e já está sinalizada pela flag COLETA_ANTES_SINTOMA.

Uso:
    python -m scripts.remover_fora_do_prazo             # dry-run
    python -m scripts.remover_fora_do_prazo --executar  # aplica
    python -m scripts.remover_fora_do_prazo --ano 2026  # restringe a um ano
"""

from __future__ import annotations

import argparse

from scripts.merge_data import MAX_DIAS_SINTOMA_COLETA
from src import db

# Só remove o que está comprovadamente ACIMA da janela: data ausente ou
# diferença negativa preservam a amostra.
_CONDICAO = (
    "data_coleta IS NOT NULL AND data_sintomas IS NOT NULL "
    "AND (data_coleta - data_sintomas) > %s"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--executar", action="store_true",
                    help="aplica as remoções (sem esta flag, apenas simula)")
    ap.add_argument("--ano", type=int, default=None,
                    help="restringe a um ano_verdade (default: todos)")
    args = ap.parse_args()

    onde = _CONDICAO
    params: list = [MAX_DIAS_SINTOMA_COLETA]
    if args.ano is not None:
        onde += " AND ano_verdade = %s"
        params.append(args.ano)

    con = db.init_db()
    try:
        total = con.execute("SELECT COUNT(*) AS n FROM amostras").fetchone()["n"]
        print(f"[1] Banco: {total} amostras")
        print(f"    Regra: remover coleta > {MAX_DIAS_SINTOMA_COLETA} dias "
              f"após o 1º sintoma" + (f" (ano {args.ano})" if args.ano else ""))

        r = con.execute(
            f"SELECT COUNT(*) AS n, MIN(data_coleta - data_sintomas) AS min_d, "
            f"MAX(data_coleta - data_sintomas) AS max_d "
            f"FROM amostras WHERE {onde}", tuple(params)
        ).fetchone()
        alvo = r["n"]
        print(f"[2] Fora do prazo: {alvo} amostras "
              f"(de {r['min_d']} a {r['max_d']} dias)")

        if not alvo:
            print("Nada a remover.")
            return 0

        # Trabalho de bancada nessas amostras. Remover algo que a equipe já
        # processou apagaria trabalho real — o operador precisa ver antes.
        marcadores = ("coletada", "extraida", "pcr_feito", "sequenciado", "rejeitada")
        b = con.execute(
            f"SELECT {', '.join(f'SUM({c}) AS {c}' for c in marcadores)}, "
            f"COUNT(data_resultado) AS com_ct, COUNT(obs_reprocesso) AS com_obs "
            f"FROM amostras WHERE {onde}", tuple(params)
        ).fetchone()
        print("[3] Trabalho de bancada nessas amostras:")
        for c in marcadores:
            print(f"      {c:12} {b[c] or 0}")
        print(f"      {'com Ct':12} {b['com_ct']}")
        print(f"      {'com obs':12} {b['com_obs']}")

        com_trabalho = sum(b[c] or 0 for c in marcadores) + b["com_ct"] + b["com_obs"]
        if com_trabalho:
            print(f"\n    ATENÇÃO: {com_trabalho} marcações de bancada seriam perdidas.")

        print("\n[4] Amostras (primeiras 10):")
        for x in con.execute(
            f"SELECT chave, data_sintomas, data_coleta, "
            f"(data_coleta - data_sintomas) AS dias FROM amostras WHERE {onde} "
            f"ORDER BY chave LIMIT 10", tuple(params)
        ).fetchall():
            print(f"      {x['chave']:12} {x['data_sintomas']} -> {x['data_coleta']} "
                  f"({x['dias']} dias)")

        if not args.executar:
            print(f"\nDRY-RUN: {alvo} seriam removidas. "
                  f"Rode com --executar para aplicar.")
            return 0

        # eventos.chave tem FK sem ON DELETE, então o histórico sai primeiro.
        n_ev = con.execute(
            f"SELECT COUNT(*) AS n FROM eventos WHERE chave IN "
            f"(SELECT chave FROM amostras WHERE {onde})", tuple(params)
        ).fetchone()["n"]
        con.execute(
            f"DELETE FROM eventos WHERE chave IN "
            f"(SELECT chave FROM amostras WHERE {onde})", tuple(params)
        )
        con.execute(f"DELETE FROM amostras WHERE {onde}", tuple(params))
        con.commit()

        restantes = con.execute("SELECT COUNT(*) AS n FROM amostras").fetchone()["n"]
        print(f"\n[5] Removidas {alvo} amostras e {n_ev} eventos.")
        print(f"    Total no banco agora: {restantes}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
