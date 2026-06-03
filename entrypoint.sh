#!/bin/sh
set -e

# Primeiro boot em produção (PostgreSQL/Neon): detecta banco vazio e popula
# a partir do xlsx bundled na imagem. O importer é idempotente, mas evitamos
# rodá-lo em cada restart verificando se já há amostras.

if [ -n "$DATABASE_URL" ]; then
    COUNT=$(python - <<'EOF'
import os, sys
try:
    import psycopg2
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM amostras")
    print(cur.fetchone()[0])
    cur.close()
    con.close()
except Exception:
    print(0)
EOF
)
    if [ "$COUNT" = "0" ]; then
        echo "[entrypoint] Banco vazio — criando schema e importando xlsx..."
        python -m src.importer
        echo "[entrypoint] Import concluído."
    else
        echo "[entrypoint] Banco Neon OK — $COUNT amostras."
    fi
fi

exec python -m src.app
