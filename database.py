# =============================================================================
# database.py - Databricks SQL Connector
#
# Keeps a small pool of long-lived connections instead of opening a brand
# new Databricks SQL Warehouse connection (a full network handshake) on
# every single query. fetch_all() borrows a connection from the pool, runs
# its query, and returns the connection to the pool afterward.
#
# A pooled connection that turns out to be dead (warehouse restarted, idle
# connection dropped, etc.) is dropped and replaced automatically, and the
# query is retried once on a fresh connection before the error is raised.
# =============================================================================

import queue
import threading
from contextlib import contextmanager

from databricks import sql
from databricks.sql.exc import Error as DatabricksError

from config import settings

POOL_SIZE = settings.databricks_pool_size
ACQUIRE_TIMEOUT_SECONDS = 30

_pool: "queue.Queue" = queue.Queue(maxsize=POOL_SIZE)
_created_count = 0
_pool_lock = threading.Lock()


def _open_connection():
    """Open one new Databricks SQL Warehouse connection."""
    return sql.connect(
        server_hostname = settings.databricks_host,
        http_path       = settings.databricks_http_path,
        access_token    = settings.databricks_token,
    )


def _acquire_connection():
    """
    Reuse an idle pooled connection if one exists, otherwise open a new
    one (up to POOL_SIZE total), otherwise wait for one to be released.
    """
    global _created_count

    try:
        return _pool.get_nowait()
    except queue.Empty:
        pass

    with _pool_lock:
        if _created_count < POOL_SIZE:
            _created_count += 1
            should_open_new = True
        else:
            should_open_new = False

    if should_open_new:
        try:
            return _open_connection()
        except Exception:
            # Opening failed - undo the reservation so a transient outage
            # doesn't permanently shrink the pool's capacity.
            with _pool_lock:
                _created_count -= 1
            raise

    try:
        return _pool.get(timeout=ACQUIRE_TIMEOUT_SECONDS)
    except queue.Empty:
        raise RuntimeError(
            "Timed out waiting for a free Databricks connection from the pool. "
            "All connections are in use."
        ) from None


def _release_connection(conn, is_healthy: bool) -> None:
    """
    Return a connection to the pool for reuse. A connection that failed is
    closed and replaced instead, so one bad connection doesn't
    permanently shrink the pool.
    """
    global _created_count

    if is_healthy:
        _pool.put(conn)
        return

    try:
        conn.close()
    except Exception:
        pass

    try:
        _pool.put(_open_connection())
    except Exception:
        # Databricks is unreachable right now. Shrink the pool by one
        # instead of leaving a permanently "checked out" slot - the next
        # caller that finds the pool empty will just open a fresh
        # connection on demand.
        with _pool_lock:
            _created_count -= 1


@contextmanager
def _borrowed_connection():
    conn = _acquire_connection()
    healthy = True
    try:
        yield conn
    except DatabricksError:
        # The query failed. This could be a bad query, or it could be a
        # dead connection - treat it as dead so a stale/expired connection
        # does not stay in the pool for the next caller to hit again.
        healthy = False
        raise
    finally:
        _release_connection(conn, healthy)


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
    list of dict  - one dict per row, keys = column names

    Reuses a small pool of long-lived connections (see module docstring)
    instead of connecting fresh on every call. If a pooled connection turns
    out to be stale, the query is retried once on a fresh connection.
    """
    last_error = None

    for attempt in (1, 2):
        try:
            with _borrowed_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params or [])
                    columns = [desc[0] for desc in cursor.description]
                    rows    = cursor.fetchall()
                    return [dict(zip(columns, row)) for row in rows]
        except DatabricksError as error:
            last_error = error

    raise last_error


def close_all_connections() -> None:
    """Close every pooled connection. Call this on application shutdown."""
    global _created_count

    while True:
        try:
            conn = _pool.get_nowait()
        except queue.Empty:
            break

        try:
            conn.close()
        except Exception:
            pass

    with _pool_lock:
        _created_count = 0
