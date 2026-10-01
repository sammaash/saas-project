"""PostgreSQL connections for the Phase 1 SaaS layer.

psycopg is imported inside functions rather than at module import, so a missing driver
(or a machine that has never installed ``requirements-saas.txt``) cannot break anything
that merely imports this package.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .config import database_url, require_database_url

# Roles this helper will switch to. Restricted to a constant set because SET ROLE cannot
# be parameterised -- the value is interpolated into SQL, so it must never come from a
# request.
ALLOWED_ROLES = frozenset({"app_backend", "authenticated", "anon"})


def connect(url: str | None = None) -> Any:
    """Open a connection with autocommit disabled.

    Autocommit off matters: the tenant-context helper depends on being inside a
    transaction so that ``set_config(..., true)`` is transaction-local and cannot leak to
    the next request served by the same pooled connection.
    """
    try:
        import psycopg  # noqa: PLC0415  (deliberately local, see module docstring)
    except ModuleNotFoundError as error:  # pragma: no cover - developer feedback path
        raise RuntimeError(
            "psycopg is not installed. Install the SaaS/development requirements first:\n"
            "    pip install -r requirements-saas.txt"
        ) from error

    return psycopg.connect(url or require_database_url())


@contextmanager
def connection(url: str | None = None) -> Iterator[Any]:
    """Yield a connection and always close it."""
    conn = connect(url)
    try:
        yield conn
    finally:
        conn.close()


def set_local_role(conn: Any, role: str) -> None:
    """Switch the current role inside the current transaction.

    ``SET LOCAL ROLE`` requires an open transaction and is undone when it ends, so a
    pooled connection never returns to the pool wearing another role. Used by the
    isolation tests to act as ``app_backend`` or ``authenticated`` exactly as PostgREST
    does in production.
    """
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Unsupported role {role!r}. Allowed: {sorted(ALLOWED_ROLES)}")
    with conn.cursor() as cursor:
        cursor.execute(f"set local role {role}")
