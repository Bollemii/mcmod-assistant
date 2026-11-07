"""Connexion à PostgreSQL + pgvector.

Accès synchrone (psycopg 3) : sous Windows, la boucle asyncio d'uvicorn n'est pas
compatible avec le mode asynchrone de psycopg. L'API passe par asyncio.to_thread.
"""
import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


# Évolutions du schéma pour une base créée avec une version antérieure de schema.sql
# (qui n'est appliqué qu'à la création du volume Docker). Idempotentes.
MIGRATIONS = [
    "ALTER TABLE queries ADD COLUMN IF NOT EXISTS chunk_keys text[]",
    "ALTER TABLE queries ADD COLUMN IF NOT EXISTS search_ms int",
    "ALTER TABLE queries ADD COLUMN IF NOT EXISTS llm_ms int",
]


def connect(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url)
    register_vector(conn)
    return conn


def migrate(conn: psycopg.Connection) -> None:
    for statement in MIGRATIONS:
        conn.execute(statement)
    conn.commit()


def make_pool(url: str) -> ConnectionPool:
    return ConnectionPool(url, min_size=1, max_size=5, configure=register_vector, open=True,
                          kwargs={"connect_timeout": 5})
