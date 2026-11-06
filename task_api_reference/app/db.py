"""Database connection dependency.

One autocommit connection per request.  Transaction boundaries are explicit:
services wrap multi-statement work in ``with conn.transaction():`` so the
commit happens before the HTTP response is produced.  (Swap in
``psycopg_pool.ConnectionPool`` for production traffic.)
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


def connect() -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row, autocommit=True)


def get_conn() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
