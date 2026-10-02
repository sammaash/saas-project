"""PostgreSQL connections for the Phase 1 SaaS layer.

psycopg is imported inside functions rather than at module import, so a missing driver
(or a machine that has never installed ``requirements.txt``) cannot break anything
that merely imports this package.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .config import app_db_role, require_database_url

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
            "    pip install -r requirements.txt"
        ) from error

    conn = psycopg.connect(url or require_database_url())
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                select current_user, role.rolsuper, role.rolbypassrls,
                       exists (
                           select 1
                             from pg_catalog.pg_roles elevated
                            where (elevated.rolsuper or elevated.rolbypassrls
                                   or elevated.rolname = 'service_role')
                              and (elevated.rolname = role.rolname
                                   or pg_catalog.pg_has_role(
                                       role.oid, elevated.oid, 'MEMBER'
                                   ))
                       )
                  from pg_catalog.pg_roles role
                 where role.rolname = current_user
                """
            )
            row = cursor.fetchone()
        if (
            row is None
            or row[0] != app_db_role()
            or row[1]
            or row[2]
            or row[3]
        ):
            actual_role = row[0] if row else "unknown"
            raise RuntimeError(
                f"Unsafe SaaS database role {actual_role!r}; expected {app_db_role()!r} "
                "without superuser, service_role, or BYPASSRLS privileges."
            )
    except Exception:
        conn.close()
        raise
    return conn


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
