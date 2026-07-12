# =============================================================================
# database.py — Databricks SQL Connector
#
# Provides a reusable connection context manager and a helper function
# that executes any query and returns results as a list of plain dicts.
# Every API route calls fetch_all() — no route ever manages a connection directly.
# =============================================================================

from contextlib import contextmanager
from databricks import sql
from config import settings


@contextmanager
def _get_connection():
    """
    Open a Databricks SQL Warehouse connection and guarantee it is closed
    when the block exits, even if an exception is raised.
    """
    conn = sql.connect(
        server_hostname = settings.databricks_host,
        http_path       = settings.databricks_http_path,
        access_token    = settings.databricks_token,
    )
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(query: str, params: list = None) -> list[dict]:
    """
    Execute a SQL query against Databricks and return all rows as a list
    of dicts, where keys are column names.

    Parameters
    ----------
    query  : SQL string (use ? placeholders for parameterized queries)
    params : Optional list of parameter values matching the ? placeholders

    Returns
    -------
    list of dict  — one dict per row, keys = column names
    """
    with _get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or [])
            columns = [desc[0] for desc in cursor.description]
            rows    = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
