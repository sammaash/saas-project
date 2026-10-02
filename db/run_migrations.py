"""Apply the SaaS platform migrations (and optional development seeds) to PostgreSQL.

Usage (local development):

    python db/run_migrations.py --include-local --seed
    python db/run_migrations.py --status

Environment:

    SAAS_DATABASE_URL   PostgreSQL connection string (or pass --database-url)

Requires psycopg 3:  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
LOCAL_DIR = REPO_ROOT / "db" / "local"
SEEDS_DIR = REPO_ROOT / "db" / "seeds"

# Tracks which migrations have been applied. Versioned by file name so ordering is
# reproducible and a modified migration is detected instead of silently ignored.
LEDGER_SQL = """
create table if not exists public.saas_schema_migrations (
  version    text primary key,
  checksum   text not null,
  applied_at timestamptz not null default now()
);
"""


def _psycopg():
    """Import psycopg lazily so a missing optional dependency gives a clear message."""
    try:
        import psycopg  # noqa: PLC0415  (deliberately local: not a production import)
    except ModuleNotFoundError:  # pragma: no cover - developer feedback path
        sys.exit(
            "psycopg is not installed. Install the SaaS/development requirements first:\n"
            "    pip install -r requirements.txt"
        )
    return psycopg


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _run_script(conn, sql: str) -> None:
    """Execute a whole .sql file as one script.

    ClientCursor uses the simple query protocol, which permits multiple statements in a
    single call -- necessary because these files contain dollar-quoted function bodies
    that a semicolon split would corrupt. The cursor is constructed positionally because
    psycopg 3.3 removed the ``cursor_factory`` keyword argument.
    """
    psycopg = _psycopg()
    cursor = psycopg.ClientCursor(conn)
    try:
        cursor.execute(sql)
    finally:
        cursor.close()


def _sql_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.sql"))


def _applied(conn) -> dict[str, str]:
    with conn.cursor() as cursor:
        cursor.execute("select version, checksum from public.saas_schema_migrations")
        return {version: checksum for version, checksum in cursor.fetchall()}


def apply_migrations(conn, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration in file-name order. Safe to re-run."""
    _run_script(conn, LEDGER_SQL)
    conn.commit()

    applied = _applied(conn)
    ran: list[str] = []

    for path in _sql_files(directory):
        sql = path.read_text(encoding="utf-8")
        digest = _checksum(sql)

        if path.name in applied:
            if applied[path.name] != digest:
                raise SystemExit(
                    f"{path.name} was modified after it was applied "
                    f"(recorded {applied[path.name]}, now {digest}).\n"
                    "Migrations are immutable once applied: add a new file instead."
                )
            print(f"skip   {path.name} (already applied)")
            continue

        print(f"apply  {path.name}")
        _run_script(conn, sql)
        with conn.cursor() as cursor:
            cursor.execute(
                "insert into public.saas_schema_migrations (version, checksum) values (%s, %s)",
                (path.name, digest),
            )
        conn.commit()
        ran.append(path.name)

    return ran

def apply_all(conn, directory: Path, label: str) -> None:
    """Apply re-runnable scripts (dev-only roles, seed data) without recording them.

    These files are written to be idempotent, so running them repeatedly is expected and
    safe -- unlike migrations, which are applied once and then immutable.
    """
    for path in _sql_files(directory):
        print(f"{label:<6} {path.name}")
        _run_script(conn, path.read_text(encoding="utf-8"))
        conn.commit()


def print_status(conn) -> None:
    """Show which migrations are applied and which are pending."""
    _run_script(conn, LEDGER_SQL)
    conn.commit()

    applied = _applied(conn)
    if not applied:
        print("no migrations recorded yet")
    else:
        print("applied migrations:")
        for version in sorted(applied):
            print(f"  {version}  ({applied[version]})")

    pending = [path.name for path in _sql_files(MIGRATIONS_DIR) if path.name not in applied]
    if pending:
        print("not applied yet:")
        for name in pending:
            print(f"  {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the Phase 1 SaaS migrations to PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("SAAS_DATABASE_URL"),
        help="PostgreSQL connection string (defaults to SAAS_DATABASE_URL)",
    )
    parser.add_argument(
        "--include-local",
        action="store_true",
        help="also apply db/local/*.sql (development/CI only; never used on Supabase)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="also apply db/seeds/*.sql (idempotent development seed data)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DESTRUCTIVE: drop the public and app schemas first (localhost only)",
    )
    parser.add_argument(
        "--i-am-sure",
        action="store_true",
        help="required confirmation for --reset; still restricted to localhost databases",
    )
    parser.add_argument(
        "--status", action="store_true", help="show applied and pending migrations"
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("set SAAS_DATABASE_URL or pass --database-url")

    if args.reset and not args.i_am_sure:
        parser.error("--reset requires the explicit confirmation flag --i-am-sure")
    if args.reset:
        try:
            database_url = urlsplit(args.database_url)
            hostname = database_url.hostname
        except ValueError:
            hostname = None
        if hostname is None:
            parser.error("--reset requires a valid localhost database URL")
        if database_url.query or database_url.fragment:
            parser.error("--reset refuses URL parameters or fragments that could override the host")
        if "supabase" in hostname.lower():
            parser.error("--reset is forbidden for Supabase URLs")
        if hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("--reset is restricted to localhost/loopback database URLs")

    psycopg = _psycopg()

    with psycopg.connect(args.database_url) as conn:
        if args.reset:
            print("!! --reset: dropping schemas public and app (localhost only)")
            _run_script(
                conn,
                "drop schema if exists public cascade; "
                "create schema public; "
                "drop schema if exists app cascade;",
            )
            conn.commit()

        if args.status:
            print_status(conn)
            return 0

        apply_migrations(conn)

        if args.include_local:
            apply_all(conn, LOCAL_DIR, "local")

        if args.seed:
            apply_all(conn, SEEDS_DIR, "seed")

    print("migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
