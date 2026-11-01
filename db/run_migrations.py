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
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
LOCAL_DIR = REPO_ROOT / "db" / "local"
SEEDS_DIR = REPO_ROOT / "db" / "seeds"

# Staging target configuration - explicitly configured expected values
# These should be set according the specific Railway staging project setup
# Hostname must match the exact Railway internal service hostname
EXPECTED_STAGING_HOSTNAME = "postgres.railway.internal"
# Database name must be exactly "railway" - do not accept "postgres" as default
EXPECTED_STAGING_DATABASE = "railway"
# Tracks which migrations have been applied. Versioned by file name so ordering is
# reproducible and a modified migration is detected instead of silently ignored.
LEDGER_SQL = """
create table if not exists public.saas_schema_migrations (
  version    text primary key,
  checksum   text not null,
  applied_at timestamptz not null default now()
);
"""


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Explicitly approved disposable database names for loopback hosts.
# These are the only database names permitted for localhost/127.0.0.1/::1 targets.
# Do not add arbitrary local database names - they will be rejected.
APPROVED_DISPOSABLE_DATABASE_NAMES = frozenset({"saas_platform"})


def parse_database_url(database_url: str) -> dict | None:
    """
    Parse and validate a PostgreSQL connection URL.

    Returns dict with validated components, or None if URL is invalid.
    Validates host and database names for recognized targets, requires port 5432
    for staging, and rejects malformed URLs or connection-target overrides.

    Does NOT connect to the database; purely string analysis.
    """
    try:
        parts = urlsplit(database_url)
        hostname = parts.hostname
        port = parts.port
    except (ValueError, TypeError):
        return None

    # Validate scheme
    if parts.scheme not in ("postgresql", "postgres"):
        return None

    # Extract and validate hostname
    if not hostname:
        return None
    hostname = hostname.lower()

    # Validate hostname
    # Accept exact expected staging hostname, or local/loopback hostnames
    # Local URLs are accepted only for the explicitly approved disposable database.
    if hostname != EXPECTED_STAGING_HOSTNAME and hostname not in LOOPBACK_HOSTS:
        return None
    if hostname == EXPECTED_STAGING_HOSTNAME and port != 5432:
        return None
    if hostname == EXPECTED_STAGING_HOSTNAME and os.environ.get("PGHOSTADDR"):
        return None

    path_segments = [segment for segment in parts.path.split("/") if segment]
    if len(path_segments) != 1:
        return None
    path_database = path_segments[0]

    if hostname in LOOPBACK_HOSTS:
        if path_database not in APPROVED_DISPOSABLE_DATABASE_NAMES:
            return None
    elif path_database != EXPECTED_STAGING_DATABASE:
        return None

    redirecting_parameters = {"host", "hostaddr", "port", "dbname", "service"}
    query = parse_qs(parts.query, keep_blank_values=True)
    if redirecting_parameters.intersection(key.lower() for key in query):
        return None
    if parts.fragment or "supabase" in database_url.lower():
        return None

    return {
        "scheme": parts.scheme,
        "hostname": hostname,
        "port": port,
        "database": path_database,
        "query": parts.query,
        "fragment": parts.fragment,
    }


def is_local_target(url: str, environ: dict[str, str] | None = None) -> bool:
    """True only when every possible connection target is loopback or a unix socket."""
    hosts = database_hosts(url, environ)
    if hosts is None:
        return False
    return all(host.lower() in LOOPBACK_HOSTS or host.startswith("/") for host in hosts)


def database_hosts(url: str, environ: dict[str, str] | None = None) -> list[str] | None:
    """Every host libpq could connect to for ``url``; None when it cannot be determined."""
    environ = os.environ if environ is None else environ
    if "://" not in url:
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        params = parse_qs(parts.query)
    except ValueError:
        return None
    hosts = [hostname] if hostname else []
    for key in ("host", "hostaddr"):
        hosts += [item for value in params.get(key, []) for item in value.split(",")]
    if not hosts:
        hosts += [item for item in environ.get("PGHOST", "").split(",") if item]
    if "hostaddr" not in params:
        hosts += [item for item in environ.get("PGHOSTADDR", "").split(",") if item]
    return [host.strip() for host in hosts]


def is_local_target(url: str, environ: dict[str, str] | None = None) -> bool:
    """True only when every possible connection target is loopback or a unix socket."""
    hosts = database_hosts(url, environ)
    if hosts is None:
        return False
    return all(host.lower() in LOOPBACK_HOSTS or host.startswith("/") for host in hosts)


def is_supabase_target(url: str, environ: dict[str, str] | None = None) -> bool:
    hosts = database_hosts(url, environ) or []
    return "supabase" in url.lower() or any("supabase" in host.lower() for host in hosts)


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


PROVISIONER_ROLE = "saas_platform_provisioner"


def ensure_provisioner_access(conn) -> bool:
    """Let a non-superuser migration role own functions as the provisioner (hosted PostgreSQL).

    ``ALTER FUNCTION ... OWNER TO`` and ``CREATE OR REPLACE`` on provisioner-owned functions
    need SET and INHERIT on that role. On PostgreSQL 16+ (Supabase) a CREATEROLE user that
    creates a role holds only ADMIN on it, so the runner creates the role with the same
    attributes migration 0013 uses and grants itself that membership for the run only.
    A superuser needs nothing. Returns True when a membership was granted (release it after).
    """
    with conn.cursor() as cursor:
        cursor.execute(
            "select rolsuper, current_setting('server_version_num')::int "
            "from pg_catalog.pg_roles where rolname = current_user"
        )
        is_superuser, version = cursor.fetchone()
        if is_superuser:
            return False
        cursor.execute("select 1 from pg_catalog.pg_roles where rolname = %s", (PROVISIONER_ROLE,))
        if cursor.fetchone() is None:
            cursor.execute(f"create role {PROVISIONER_ROLE} nologin noinherit nobypassrls")
        if version >= 160000:
            cursor.execute(
                "select pg_catalog.pg_has_role(current_user, %s, 'USAGE') "
                "and pg_catalog.pg_has_role(current_user, %s, 'SET')",
                (PROVISIONER_ROLE, PROVISIONER_ROLE),
            )
            if cursor.fetchone()[0]:
                conn.commit()
                return False
            cursor.execute(f"grant {PROVISIONER_ROLE} to current_user with inherit true, set true")
        else:
            cursor.execute(
                "select pg_catalog.pg_has_role(current_user, %s, 'MEMBER')", (PROVISIONER_ROLE,)
            )
            if cursor.fetchone()[0]:
                conn.commit()
                return False
            cursor.execute(f"grant {PROVISIONER_ROLE} to current_user")
    conn.commit()
    return True


def release_provisioner_access(conn) -> None:
    """Drop the temporary membership granted by :func:`ensure_provisioner_access`."""
    with conn.cursor() as cursor:
        cursor.execute(f"revoke {PROVISIONER_ROLE} from current_user")
    conn.commit()


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


def _ledger_exists(conn) -> bool:
    """True when the migration ledger exists.

    A database that has never been migrated has no ledger table, and ``--status`` is the
    first command an operator runs against it, so it must describe that state instead of
    failing with ``UndefinedTable``.
    """
    with conn.cursor() as cursor:
        cursor.execute("select to_regclass('public.saas_schema_migrations')")
        return cursor.fetchone()[0] is not None


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
    applied = _applied(conn) if _ledger_exists(conn) else {}

    if not applied:
        print("no migrations recorded yet")
    else:
        print("applied migrations:")
        for version in sorted(applied):
            print(f"  {version}  ({applied[version]})")

    files = _sql_files(MIGRATIONS_DIR)
    pending = [
        path.name for path in files
        if path.name not in applied
    ]

    if pending:
        print("not applied yet:")
        for name in pending:
            print(f"  {name}")

    mismatches = [
        (path.name, applied[path.name], _checksum(path.read_text(encoding="utf-8")))
        for path in files
        if path.name in applied
        and applied[path.name] != _checksum(path.read_text(encoding="utf-8"))
    ]

    if mismatches:
        print("CHECKSUM MISMATCHES:")
        for name, recorded, current in mismatches:
            print(f"  {name}: recorded={recorded}, current={current}")



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
    parser.add_argument(
        "--allow-nonlocal-dev",
        action="store_true",
        help="development override: allow --include-local/--seed on a non-loopback host "
        "(never honoured for Supabase hosts)",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("set SAAS_DATABASE_URL or pass --database-url")

    # Validate every target before importing psycopg or opening a connection.
    if is_supabase_target(args.database_url):
        parser.error("Supabase database target is not supported")

    parsed_url = parse_database_url(args.database_url)
    if parsed_url is None:
        parser.error(
            "database target is not an approved loopback disposable database "
            "or the expected staging database"
        )
    hostname = parsed_url["hostname"]
    if hostname in LOOPBACK_HOSTS:
        if not is_local_target(args.database_url):
            parser.error("loopback database URL resolves to a non-loopback host")
    elif not validate_staging_target(parsed_url):
        parser.error("database target does not match the expected staging target")

    # === PER-FLAG CHECKS FOLLOWING STAGING VALIDATION ===
    dev_data = args.include_local or args.seed
    if dev_data:
        if is_supabase_target(args.database_url):
            parser.error(
                "--include-local/--seed set known development passwords and create fixture "
                "data; they are forbidden for Supabase databases"
            )
        if not is_local_target(args.database_url) and not args.allow_nonlocal_dev:
            parser.error(
                "--include-local/--seed are refused for non-local database hosts; pass "
                "--allow-nonlocal-dev only for a disposable development database"
            )

    if args.reset and not args.i_am_sure:
        parser.error("--reset requires the explicit confirmation flag --i-am-sure")
    if args.reset:
        try:
            database_url = urlsplit(args.database_url)
            reset_hostname = database_url.hostname
        except ValueError:
            reset_hostname = None
        if reset_hostname is None:
            parser.error("--reset requires a valid localhost database URL")
        if database_url.query or database_url.fragment:
            parser.error("--reset refuses URL parameters or fragments that could override the host")
        if "supabase" in (reset_hostname or "").lower():
            parser.error("--reset is forbidden for Supabase URLs")
        if (reset_hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("--reset is restricted to localhost/loopback database URLs")

    # Now proceed to connect – staging target has already been validated
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

        # Recheck the staging identity immediately before migrations; local targets
        # were restricted to the disposable-name allowlist before connecting.
        if hostname not in LOOPBACK_HOSTS and not validate_staging_target(parsed_url):
            print("FAIL: Staging target validation failed - blocking migration")
            sys.exit(1)

        # Check runtime database identity (fail-closed): verify we're connected to the intended database
        if not check_runtime_database_identity(conn, parsed_url["database"]):
            print("FAIL: Runtime database identity check failed - blocking migration")
            sys.exit(1)

        granted = ensure_provisioner_access(conn)
        try:
            apply_migrations(conn)

            if dev_data:
                # The db/local and db/seeds scripts refuse to run without this session setting.
                conn.execute("select set_config('saas.allow_dev_data', 'on', false)")

            if args.include_local:
                apply_all(conn, LOCAL_DIR, "local")

            if args.seed:
                apply_all(conn, SEEDS_DIR, "seed")
        finally:
            if granted:
                conn.rollback()
                release_provisioner_access(conn)

    print("migrations complete")
    return 0


def validate_staging_target(parsed_url: dict | None) -> bool:
    """
    Validate that a parsed database URL represents the expected staging target.

    Performs independent checks for:
    - Hostname matches expected staging service hostname
    - Database name is exactly the expected staging database
    - No production indicators that can be verified
    - Supabase is not the target

    Returns True only when ALL checks pass. Blocks execution if any check fails.
    """
    if parsed_url is None:
        print("FAIL: Could not parse or validate database URL")
        return False

    hostname = parsed_url["hostname"]
    database = parsed_url["database"]

    # Check 1: Hostname must match expected staging service hostname exactly
    # Do not rely on hostname suffix alone or absence of "prod" words
    expected_hostname = EXPECTED_STAGING_HOSTNAME
    if hostname != expected_hostname:
        print(f"FAIL: Hostname '{hostname}' does not match expected staging hostname '{expected_hostname}'")
        print("Do not rely on hostname suffix alone or absence of 'prod' words as proof of staging identity")
        return False

    # Check 2: Database name must be exactly the expected staging database
    # Do not accept "postgres" as alternative without independent evidence
    if database != EXPECTED_STAGING_DATABASE:
        print(f"FAIL: Database name '{database}' does not match expected staging database '{EXPECTED_STAGING_DATABASE}'")
        print("Do not accept 'postgres' as default without independent evidence that it is the intended database")
        return False

    # All checks passed
    print(f"PASS: Staging target validation successful")
    print(f"  Host: {hostname}")
    print(f"  Database: {database}")
    return True


def check_runtime_database_identity(conn, expected_database: str) -> bool:
    """
    Runtime identity check for the connected database.

    This is a separate runtime check that must fail closed if the actual
    database identity cannot be verified. It operates on an active database
    connection and checks the database's identity metadata.

    Returns True only when database identity can be positively verified.
    Returns False and blocks execution if identity cannot be verified.

    Note: This check requires psycopg connection and should be called after
    establishing the connection, before applying migrations.
    """
    try:
        with conn.cursor() as cursor:
            # Check the database name by querying the current database
            cursor.execute("SELECT current_database()")
            current_db = cursor.fetchone()[0]

            if current_db != expected_database:
                print(
                    f"FAIL: Current database '{current_db}' does not match "
                    f"the requested database '{expected_database}'"
                )
                print("Cannot verify database identity - blocking for safety")
                return False

            # If we get here, basic identity checks passed
            print(f"PASS: Runtime database identity check passed (database='{current_db}')")
            return True

    except Exception as e:
        # If any error occurs during the identity check, fail closed
        print(f"FAIL: Runtime database identity check error: {e}")
        print("Cannot verify database identity - blocking for safety")
        return False
