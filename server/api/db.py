"""Connexion à PostgreSQL + pgvector.

Accès synchrone (psycopg 3) : sous Windows, la boucle asyncio d'uvicorn n'est pas
compatible avec le mode asynchrone de psycopg. L'API passe par asyncio.to_thread.
"""
import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


def connect(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url)
    register_vector(conn)
    return conn


def make_pool(url: str) -> ConnectionPool:
    return ConnectionPool(url, min_size=1, max_size=5, configure=register_vector, open=True,
                          kwargs={"connect_timeout": 5})
