#!/bin/sh
set -e

# Primeiro boot em produção (PostgreSQL/Neon): detecta banco vazio e popula
# em background para que o app suba e abra a porta imediatamente.
# O Render exige que a porta esteja aberta durante o scan de inicialização.

if [ -n "$DATABASE_URL" ]; then
    COUNT=$(python - <<'EOF'
import os
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
        echo "[entrypoint] Banco vazio — importando em background (app sobe já)..."
        python -m src.importer &
    else
        echo "[entrypoint] Banco Neon OK — $COUNT amostras."
    fi
fi

exec python -m src.app
