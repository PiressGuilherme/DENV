"""Gera uma planilha de EXEMPLO de resultados de PCR para testar o import.

Lê amostras que já estão em 'PCR feito' e ainda não têm resultado, e monta um
arquivo no formato esperado (NI, DEN1..DEN4) com valores de Ct plausíveis. Como
os NIs vêm do próprio banco, o import encontra as amostras de verdade — uma
planilha com NIs inventados só exercitaria o caminho de erro.

O script é READ-ONLY: apenas lê o banco e escreve arquivos locais. Nada é
gravado em `amostras`; a escrita só acontece se você confirmar o import na UI.

Uso:
    # contra um banco de teste (recomendado — ver README)
    DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/denv_test" \
        python -m scripts.gerar_planilha_exemplo

    python -m scripts.gerar_planilha_exemplo --n 50 --saida data/exemplos
    python -m scripts.gerar_planilha_exemplo --com-problemas   # inclui casos-limite
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import pandas as pd

from src import db

RAIZ = Path(__file__).resolve().parent.parent
SAIDA_PADRAO = RAIZ / "data" / "exemplos"

# Proporções aproximadas de um lote real de dengue no RS: a maioria detecta um
# sorotipo só, uma fração é negativa e coinfecção é rara.
PESO_UM_SOROTIPO = 0.75
PESO_NEGATIVO = 0.20
# o restante (~5%) vira coinfecção

CT_MIN_REAL, CT_MAX_REAL = 15.0, 36.0


def _ct() -> float:
    """Ct plausível de amostra positiva (uma casa decimal)."""
    return round(random.uniform(CT_MIN_REAL, CT_MAX_REAL), 1)


def _linha_resultado(ni: str, rng: random.Random) -> dict:
    """Uma linha de resultado com sorteio ponderado do desfecho."""
    linha: dict[str, object] = {"NI": ni}
    for sorotipo in db.SOROTIPOS:
        linha[sorotipo] = None

    sorteio = rng.random()
    if sorteio < PESO_NEGATIVO:
        return linha  # todos vazios = não detectado
    if sorteio < PESO_NEGATIVO + PESO_UM_SOROTIPO:
        escolhidos = rng.sample(db.SOROTIPOS, 1)
    else:
        escolhidos = rng.sample(db.SOROTIPOS, 2)  # coinfecção
    for sorotipo in escolhidos:
        linha[sorotipo] = _ct()
    return linha


def _casos_problematicos(
    ni_para_ct_invalido: str | None,
    ni_fora_de_fase: str | None,
    ni_para_duplicar: str | None,
) -> list[dict]:
    """Linhas que exercitam cada motivo de exclusão do relatório de import.

    A triagem resolve o NI ANTES de validar o Ct, então demonstrar 'Ct inválido'
    e 'Fora da fase' exige NIs que existem de verdade no banco — com NI falso
    a linha pararia antes, em 'NI não encontrado'.
    """
    vazio: dict[str, object] = {s: None for s in db.SOROTIPOS}
    casos: list[dict] = [
        {"NI": "D999999/25", **vazio, "DEN1": 22.4},   # NI não encontrado
        {"NI": "sem barra",  **vazio, "DEN2": 25.0},   # NI inválido
        {"NI": None,         **vazio, "DEN1": 20.0},   # NI ausente
    ]
    if ni_para_ct_invalido:
        casos.append({"NI": ni_para_ct_invalido, **vazio, "DEN1": "abc"})
    if ni_fora_de_fase:
        casos.append({"NI": ni_fora_de_fase, **vazio, "DEN2": 26.0})
    if ni_para_duplicar:
        # 2ª ocorrência do mesmo NI: deve ser reportada como duplicata.
        casos.append({"NI": ni_para_duplicar, **vazio, "DEN3": 31.0})
    return casos


def _ni(linha) -> str:
    return linha["ni_original"] or linha["chave"]


def _buscar_amostras(con, n: int) -> list[str]:
    """NIs de amostras em 'PCR feito' ainda sem resultado."""
    where = db._combinar_where(
        db.where_por_fase(db.ETAPA_RESULTADO), "data_resultado IS NULL"
    )
    return [_ni(r) for r in db.listar_amostras(con, where=where)[:n]]


def _buscar_fora_de_fase(con) -> str | None:
    """Um NI que existe mas ainda não fez PCR — para demonstrar o motivo."""
    linhas = db.listar_amostras(con, where=db.where_por_fase(db.FASE_PENDENTE))
    return _ni(linhas[0]) if linhas else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30,
                        help="quantas amostras válidas incluir (padrão: 30)")
    parser.add_argument("--saida", type=Path, default=SAIDA_PADRAO,
                        help=f"diretório de saída (padrão: {SAIDA_PADRAO})")
    parser.add_argument("--com-problemas", action="store_true",
                        help="acrescenta linhas com erro para testar o relatório")
    parser.add_argument("--seed", type=int, default=42,
                        help="semente do sorteio (padrão: 42, reproduzível)")
    args = parser.parse_args(argv[1:])

    if not db._DATABASE_URL:
        print("ERRO: DATABASE_URL não definido.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    random.seed(args.seed)

    con = db.conectar()
    try:
        # Uma a mais: a última fica reservada para a linha de 'Ct inválido'.
        nis = _buscar_amostras(con, args.n + (1 if args.com_problemas else 0))
        ni_fora_de_fase = _buscar_fora_de_fase(con) if args.com_problemas else None
    finally:
        con.close()

    ni_ct_invalido = None
    if args.com_problemas and len(nis) > args.n:
        ni_ct_invalido = nis.pop()

    if not nis:
        print(
            "Nenhuma amostra em 'PCR feito' sem resultado foi encontrada.\n"
            "Marque algumas amostras como PCR feito na UI (ou rode o seed de "
            "teste) antes de gerar a planilha.",
            file=sys.stderr,
        )
        return 1
    if len(nis) < args.n:
        print(f"AVISO: só {len(nis)} amostra(s) elegíveis (pedidas {args.n}).")

    linhas = [_linha_resultado(ni, rng) for ni in nis]
    if args.com_problemas:
        linhas += _casos_problematicos(
            ni_ct_invalido, ni_fora_de_fase, nis[0] if nis else None
        )

    df = pd.DataFrame(linhas, columns=["NI", *db.SOROTIPOS])
    args.saida.mkdir(parents=True, exist_ok=True)
    sufixo = "_com_problemas" if args.com_problemas else ""
    xlsx = args.saida / f"resultados_exemplo{sufixo}.xlsx"
    csv = args.saida / f"resultados_exemplo{sufixo}.csv"
    df.to_excel(xlsx, index=False)
    df.to_csv(csv, index=False, encoding="utf-8-sig")

    positivas = sum(
        1 for l in linhas if any(l.get(s) not in (None, "abc") for s in db.SOROTIPOS)
    )
    print(f"Gerados {len(linhas)} registros ({len(nis)} de amostras reais).")
    print(f"  positivas ~{positivas} · arquivos: {xlsx.name}, {csv.name}")
    print(f"  em: {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
