#!/usr/bin/env bash
# Aplica em produção, na ordem: backup -> dry-run -> remoção -> import.
#
# Requer DATABASE_URL do Neon no ambiente:
#     export DATABASE_URL="postgresql://...@ep-xxx.neon.tech/neondb?sslmode=require"
#     bash scripts/aplicar_producao.sh
#
# Para revisar sem alterar nada (só backup + dry-run):
#     bash scripts/aplicar_producao.sh --simular
#
# O backup usa pg_dump via Docker, evitando instalar o cliente localmente. A
# major do cliente é detectada a partir do servidor: o pg_dump recusa rodar
# contra servidor de major maior (o Neon está em 18.x). Sem backup gravado, o
# script não prossegue — um DELETE não tem desfazer.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERRO: DATABASE_URL não definida." >&2
    exit 1
fi

SIMULAR=0
[ "${1:-}" = "--simular" ] && SIMULAR=1

BACKUP="backups/amostras_$(date +%Y%m%d_%H%M%S).sql"
mkdir -p backups

echo "=============================================="
echo " [1/4] BACKUP"
echo "=============================================="
# Casa a major do cliente com a do servidor (pg_dump recusa servidor mais novo).
PG_MAJOR=$(python -c "
from src import db
con = db.conectar()
print(con.execute('SHOW server_version').fetchone()['server_version'].split('.')[0])
con.close()
")
echo "Servidor PostgreSQL: $PG_MAJOR — usando postgres:${PG_MAJOR}-alpine"

docker run --rm --network host "postgres:${PG_MAJOR}-alpine" \
    pg_dump "$DATABASE_URL" --table=amostras --table=eventos \
    --no-owner --no-privileges > "$BACKUP"

if [ ! -s "$BACKUP" ]; then
    echo "ERRO: backup vazio — abortando antes de qualquer alteração." >&2
    rm -f "$BACKUP"
    exit 1
fi
echo "Backup: $BACKUP ($(du -h "$BACKUP" | cut -f1), $(grep -c '^INSERT\|^COPY' "$BACKUP" || true) blocos de dados)"

echo
echo "=============================================="
echo " [2/4] DRY-RUN da remoção"
echo "=============================================="
python -m scripts.remover_pcr_anterior

if [ "$SIMULAR" = "1" ]; then
    echo
    echo "MODO SIMULAÇÃO: parando aqui. Backup preservado em $BACKUP"
    exit 0
fi

echo
echo "=============================================="
echo " [3/4] REMOÇÃO"
echo "=============================================="
python -m scripts.remover_pcr_anterior --executar

echo
echo "=============================================="
echo " [4/4] IMPORT"
echo "=============================================="
python -m src.importer data/dengue_consolidado_pendentes.xlsx

echo
echo "=============================================="
echo " CONFERÊNCIA FINAL"
echo "=============================================="
python - <<'PY'
from pathlib import Path
from scripts.remover_pcr_anterior import chaves_com_pcr_concluida
from src import db

con = db.init_db()
try:
    total = con.execute("SELECT COUNT(*) AS n FROM amostras").fetchone()["n"]
    print(f"Total de amostras: {total}")
    for r in con.execute(
        "SELECT ano_verdade, COUNT(*) AS n FROM amostras "
        "GROUP BY ano_verdade ORDER BY ano_verdade"
    ).fetchall():
        print(f"  {r['ano_verdade']}: {r['n']}")

    chaves, _ = chaves_com_pcr_concluida(Path("data/dengue_consolidado.xlsx"))
    existentes = {r["chave"] for r in con.execute("SELECT chave FROM amostras").fetchall()}
    resid = chaves & existentes
    print(f"\nAmostras com PCR concluída ainda no banco: {len(resid)} (esperado 0)")

    dup = con.execute(
        "SELECT COUNT(*) AS n FROM (SELECT prefixo, numero_sequencial FROM amostras "
        "GROUP BY prefixo, numero_sequencial HAVING COUNT(*) > 1) t"
    ).fetchone()["n"]
    print(f"Amostras duplicadas entre anos: {dup} (esperado 0)")
finally:
    con.close()
PY

echo
echo "Concluído. Backup em $BACKUP"
