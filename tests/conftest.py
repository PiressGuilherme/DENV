"""Fixtures compartilhadas — schema PostgreSQL isolado por teste.

Requer DATABASE_URL no ambiente. Sem ela os testes de banco são pulados.
Em dev: export DATABASE_URL="postgresql://..." (branch dev do Neon).
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from src import db


def _criar_schema_isolado(scope_label: str = "fn"):
    """Abre conexão, cria schema temporário, retorna _Conn pronta com schema ativo."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL não configurado — definir para rodar testes de banco")

    schema = f"_t{scope_label}_{uuid.uuid4().hex[:8]}"
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
    raw.autocommit = False

    conn = db._Conn(raw)
    db.criar_schema(conn)
    return conn, raw, schema


@pytest.fixture
def _pg_schema_con():
    """Conexão com schema PostgreSQL isolado (function-scoped). Faz cleanup ao fim."""
    conn, raw, schema = _criar_schema_isolado("fn")
    yield conn
    try:
        raw.rollback()
    except Exception:
        pass
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    raw.close()


@pytest.fixture(scope="module")
def _pg_schema_con_module():
    """Conexão com schema PostgreSQL isolado (module-scoped). Usado pelo importer."""
    conn, raw, schema = _criar_schema_isolado("mod")
    yield conn
    try:
        raw.rollback()
    except Exception:
        pass
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    raw.close()
