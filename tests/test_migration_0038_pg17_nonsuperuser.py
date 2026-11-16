"""Migration 0038 under the production (Supabase) permission model, on real PostgreSQL 17.

Production runs migrations as a NON-superuser ``postgres`` whose membership in
``saas_platform_provisioner`` is the PostgreSQL 16+ CREATEROLE self-grant: ADMIN only,
INHERIT=false, SET=false (grantor = the bootstrap superuser, ``supabase_admin`` on Supabase).
On that model ``ALTER FUNCTION ... OWNER TO saas_platform_provisioner`` needs:

  * SET on the provisioner  -- supplied for the run by ``run_migrations.ensure_provisioner_access``
  * CREATE on schema ``app`` for the provisioner -- supplied by the migration's own
    temporary ``grant create`` / ``revoke create`` bracket (0013/0020/0021/0023/0024/0040).

A superuser (CI's ``postgres:16`` container, Railway staging) bypasses both checks, so the
ordinary database suite cannot catch a missing bracket. This module therefore starts its OWN
throwaway PostgreSQL 17 cluster whose bootstrap superuser is ``supabase_admin`` and whose
``postgres`` is a plain CREATEROLE/BYPASSRLS login -- the production role model -- and drives
the real migration runner against it. Nothing here reads SAAS_TEST_DATABASE_URL or connects
anywhere except 127.0.0.1 on a private, freshly allocated port.

Requirements: PostgreSQL 17 server binaries (``initdb``, ``pg_ctl``). They are located via
SAAS_PG17_BIN or common install paths. Without them the module skips, unless
SAAS_REQUIRE_PG17_REGRESSION=1, which turns the missing binaries into a failure.

Run:
    python -m pytest tests/test_migration_0038_pg17_nonsuperuser.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from db import run_migrations as runner  # noqa: E402  (the real migration runner)
from db.build_migration_bundle import build_bundle  # noqa: E402
from db.build_tenant_reset import build_reset  # noqa: E402

MIGRATIONS_DIR = REPO / "db" / "migrations"
MIGRATION_0038 = MIGRATIONS_DIR / "0038_my_active_tenants.sql"
PROVISIONER = "saas_platform_provisioner"
BOOTSTRAP = "supabase_admin"
HOST = "127.0.0.1"
RUNNER_DATABASE = "saas_platform"  # the only loopback name run_migrations.main() accepts
PRE_0038_TEMPLATE = "saas_platform_pre0038"

TEMP_CREATE_GRANT = "grant create on schema app to saas_platform_provisioner;\n"
TEMP_CREATE_REVOKE = "revoke create on schema app from saas_platform_provisioner;\n"

# The four operator-owned SECURITY DEFINER helpers documented as by-design exceptions.
DOCUMENTED_OPERATOR_OWNED_DEFINERS = {
    "app.is_platform_admin()",
    "app.is_tenant_member(uuid)",
    "app.is_tenant_owner(uuid)",
    "app.resolve_whatsapp_channel(text)",
}
# Production at rest (A3 inspection, rows 72/73): ADMIN-only self-grant from the bootstrap.
PRODUCTION_MEMBERSHIP = [
    ("postgres", PROVISIONER, BOOTSTRAP, True, False, False),
]
PROVISIONER_ATTRIBUTES = {
    "rolsuper": False, "rolinherit": False, "rolcreaterole": False,
    "rolbypassrls": False, "rolcanlogin": False,
}
EXPECTED_SEARCH_PATH = ["search_path=pg_catalog, public, pg_temp"]


def _pg17_bin() -> Path | None:
    exe = ".exe" if os.name == "nt" else ""
    candidates = [os.getenv("SAAS_PG17_BIN")] if os.getenv("SAAS_PG17_BIN") else []
    candidates += [
        r"C:\Program Files\PostgreSQL\17\bin",
        "/usr/lib/postgresql/17/bin",
        "/usr/pgsql-17/bin",
        "/opt/homebrew/opt/postgresql@17/bin",
        "/usr/local/opt/postgresql@17/bin",
    ]
    for candidate in candidates:
        directory = Path(candidate)
        if not all((directory / f"{tool}{exe}").is_file() for tool in ("initdb", "pg_ctl", "postgres")):
            continue
        version = subprocess.run(
            [str(directory / f"postgres{exe}"), "--version"], capture_output=True, text=True
        ).stdout
        if re.search(r"\b17\.\d+", version):
            return directory
    return None


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Migration0038NonSuperuserPg17Tests(unittest.TestCase):
    """Production-equivalent permission model; every scenario starts from a fresh database."""

    @classmethod
    def setUpClass(cls):
        try:
            import psycopg  # noqa: F401,PLC0415
        except ModuleNotFoundError:  # pragma: no cover
            raise unittest.SkipTest("psycopg is not installed")
        cls.bin = _pg17_bin()
        if cls.bin is None:
            message = "PostgreSQL 17 server binaries not found (set SAAS_PG17_BIN)"
            if os.getenv("SAAS_REQUIRE_PG17_REGRESSION") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)

        cls.workdir = Path(tempfile.mkdtemp(prefix="saas-0038-pg17-"))
        cls.addClassCleanup(shutil.rmtree, cls.workdir, ignore_errors=True)
        cls.data = cls.workdir / "data"
        cls.port = _free_loopback_port()
        exe = ".exe" if os.name == "nt" else ""
        subprocess.run(
            [str(cls.bin / f"initdb{exe}"), "-D", str(cls.data), "-U", BOOTSTRAP,
             "-A", "trust", "--no-locale", "-E", "UTF8"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        socket_dir = "" if os.name == "nt" else str(cls.workdir).replace("'", "''")
        with (cls.data / "postgresql.auto.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                f"port = {cls.port}\nlisten_addresses = '{HOST}'\n"
                f"unix_socket_directories = '{socket_dir}'\nfsync = off\n"
                "synchronous_commit = off\nfull_page_writes = off\n"
            )
        pg_ctl = str(cls.bin / f"pg_ctl{exe}")
        subprocess.run(
            [pg_ctl, "-D", str(cls.data), "-l", str(cls.workdir / "server.log"), "-w", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Registered after a successful start; class cleanups also run when setUpClass fails.
        cls.addClassCleanup(
            subprocess.run, [pg_ctl, "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        with cls._connect(BOOTSTRAP, "postgres", autocommit=True) as conn:
            cls.server_version = conn.execute("select version()").fetchone()[0]
            cls.server_version_num = int(conn.execute("show server_version_num").fetchone()[0])
            # The Supabase operator: NOT a superuser.
            conn.execute("create role postgres login createrole createdb bypassrls inherit")
            conn.execute("create role anon nologin inherit")
            conn.execute("create role authenticated nologin inherit")
            conn.execute("create role service_role nologin inherit bypassrls")
            conn.execute("create role authenticator login noinherit")
            # Supabase grants everything the migration role creates in schema public to
            # the Data API roles (production inventory 2026-11-16). Without this the
            # model cannot see a migration that forgets to revoke from them.
        # Default ACLs are per database: set them in template1 so every database created
        # below (template and per-test copies) carries them, as a Supabase project does.
        with cls._connect(BOOTSTRAP, "template1", autocommit=True) as conn:
            for kind in ("tables", "sequences", "functions"):
                conn.execute(
                    f"alter default privileges for role postgres in schema public"
                    f" grant all on {kind} to anon, authenticated, service_role"
                )
        with cls._connect(BOOTSTRAP, "postgres", autocommit=True) as conn:
            conn.execute(f"create database {PRE_0038_TEMPLATE} owner postgres")

        # Bring the template to production's state (ledger 0001-0037) with the real runner,
        # exactly as production was migrated.
        cls.pre0038_dir = cls.workdir / "pre0038"
        cls.pre0038_dir.mkdir()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name < "0038":
                shutil.copyfile(path, cls.pre0038_dir / path.name)
        error = cls._run_with_runner(PRE_0038_TEMPLATE, cls.pre0038_dir, elevate=True)
        if error is not None:
            raise RuntimeError(f"0001-0037 did not apply as a non-superuser: {error!r}")

    # ------------------------------------------------------------------ helpers
    @classmethod
    def _connect(cls, user: str, dbname: str, autocommit: bool = False):
        import psycopg  # noqa: PLC0415

        assert HOST == "127.0.0.1"
        return psycopg.connect(
            host=HOST, port=cls.port, user=user, dbname=dbname, autocommit=autocommit,
            connect_timeout=10,
        )

    @classmethod
    def _run_with_runner(cls, dbname: str, directory: Path, elevate: bool):
        """Apply ``directory`` the way run_migrations.main() does; return the error or None."""
        import psycopg  # noqa: PLC0415

        with cls._connect("postgres", dbname) as conn:
            granted = runner.ensure_provisioner_access(conn) if elevate else False
            try:
                runner.apply_migrations(conn, directory)
                return None
            except psycopg.Error as error:
                return error
            finally:
                if granted:
                    conn.rollback()
                    runner.release_provisioner_access(conn)

    def _fresh_runner_database(self, from_template: bool = True) -> str:
        with self._connect(BOOTSTRAP, "postgres", autocommit=True) as conn:
            conn.execute(f"drop database if exists {RUNNER_DATABASE} with (force)")
            template = f" template {PRE_0038_TEMPLATE}" if from_template else ""
            conn.execute(f"create database {RUNNER_DATABASE}{template} owner postgres")
        return RUNNER_DATABASE

    def _migrations_through_0038(self, text_0038: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="m", dir=self.workdir))
        for path in self.pre0038_dir.iterdir():
            shutil.copyfile(path, directory / path.name)
        (directory / MIGRATION_0038.name).write_text(text_0038, encoding="utf-8", newline="")
        return directory

    def _state(self, dbname: str) -> dict:
        with self._connect(BOOTSTRAP, dbname) as conn:
            membership = conn.execute(
                "select pg_get_userbyid(member), pg_get_userbyid(roleid), pg_get_userbyid(grantor),"
                "       admin_option, inherit_option, set_option"
                "  from pg_auth_members"
                f" where roleid = '{PROVISIONER}'::regrole or member = '{PROVISIONER}'::regrole"
                " order by 1, 2, 3"
            ).fetchall()
            effective = conn.execute(
                f"select pg_has_role('postgres', '{PROVISIONER}', 'USAGE'),"
                f"       pg_has_role('postgres', '{PROVISIONER}', 'SET')"
            ).fetchone()
            create_on_app = conn.execute(
                f"select has_schema_privilege('{PROVISIONER}', 'app', 'CREATE')"
            ).fetchone()[0]
            app_acl = conn.execute(
                "select nspacl::text from pg_namespace where nspname = 'app'"
            ).fetchone()[0]
            function = conn.execute(
                "select pg_get_userbyid(proowner), prosecdef, proconfig, proacl::text,"
                "       pg_get_function_result(oid), pg_get_function_identity_arguments(oid)"
                "  from pg_proc where oid = to_regprocedure('app.my_active_tenants()')"
            ).fetchone()
            ledger = dict(conn.execute(
                "select version, checksum from public.saas_schema_migrations"
            ).fetchall())
            superusers = {r[0] for r in conn.execute("select rolname from pg_roles where rolsuper")}
            provisioner = conn.execute(
                "select rolsuper, rolinherit, rolcreaterole, rolbypassrls, rolcanlogin"
                f"  from pg_roles where rolname = '{PROVISIONER}'"
            ).fetchone()
            operator_owned_definers = {
                r[0] for r in conn.execute(
                    "select oid::regprocedure::text from pg_proc"
                    " where pronamespace = 'app'::regnamespace and prosecdef"
                    f"   and proowner <> '{PROVISIONER}'::regrole"
                )
            }
        return {
            "membership": membership,
            "usage": effective[0], "set": effective[1],
            "provisioner_create_on_app": create_on_app,
            "app_acl": app_acl,
            "function": function,
            "ledger": ledger,
            "superusers": superusers,
            "provisioner": dict(zip(PROVISIONER_ATTRIBUTES, provisioner)),
            "operator_owned_definers": operator_owned_definers,
        }

    def _assert_production_at_rest(self, state: dict, baseline_acl: str):
        """The production role model is intact: no residual elevation of any kind."""
        self.assertEqual(state["membership"], PRODUCTION_MEMBERSHIP)
        self.assertFalse(state["usage"], "postgres must not inherit the provisioner's privileges")
        self.assertFalse(state["set"], "postgres must not be able to SET ROLE to the provisioner")
        self.assertFalse(state["provisioner_create_on_app"], "temporary CREATE leaked")
        self.assertEqual(state["app_acl"], baseline_acl)
        self.assertEqual(state["superusers"], {BOOTSTRAP})
        self.assertEqual(state["provisioner"], PROVISIONER_ATTRIBUTES)

    # ------------------------------------------------------------------ tests
    def test_0_cluster_reproduces_the_production_permission_model(self):
        self.assertEqual(self.server_version_num // 10000, 17, self.server_version)
        state = self._state(self._fresh_runner_database())
        self._assert_production_at_rest(state, state["app_acl"])
        self.assertIsNone(state["function"], "template must stop at 0037")
        self.assertEqual(max(state["ledger"]), "0037_quotation_briefs_and_deliveries.sql")
        with self._connect(BOOTSTRAP, RUNNER_DATABASE) as conn:
            self.assertEqual(
                conn.execute(
                    "select pg_get_userbyid(nspowner), has_schema_privilege(%s, 'app', 'USAGE')"
                    " from pg_namespace where nspname = 'app'", (PROVISIONER,)
                ).fetchone(),
                ("postgres", True),
            )
            self.assertEqual(
                conn.execute(
                    "select rolsuper, rolcreaterole, rolbypassrls from pg_roles where rolname = 'postgres'"
                ).fetchone(),
                (False, True, True),
            )

    def test_a_0038_without_the_create_bracket_fails_through_the_runner(self):
        """The original (uncorrected) form: runner SET/INHERIT alone is not enough."""
        import psycopg  # noqa: PLC0415

        uncorrected = MIGRATION_0038.read_text(encoding="utf-8")
        for statement in (TEMP_CREATE_GRANT, TEMP_CREATE_REVOKE):
            uncorrected = uncorrected.replace(statement, "")
        self.assertNotIn("grant create on schema app", uncorrected.lower())
        database = self._fresh_runner_database()
        baseline = self._state(database)

        error = self._run_with_runner(database, self._migrations_through_0038(uncorrected), elevate=True)

        self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        self.assertEqual(error.diag.message_primary, "permission denied for schema app")
        state = self._state(database)
        self.assertIsNone(state["function"], "a failed migration must leave no function behind")
        self.assertNotIn(MIGRATION_0038.name, state["ledger"])
        self._assert_production_at_rest(state, baseline["app_acl"])

    def test_b_temporary_create_without_provisioner_set_access_fails(self):
        """The corrected file without the runner's SET grant: CREATE alone is not enough."""
        import psycopg  # noqa: PLC0415

        database = self._fresh_runner_database()
        baseline = self._state(database)

        error = self._run_with_runner(
            database, self._migrations_through_0038(MIGRATION_0038.read_text(encoding="utf-8")),
            elevate=False,
        )

        self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        self.assertEqual(error.diag.message_primary, f'must be able to SET ROLE "{PROVISIONER}"')
        state = self._state(database)
        self.assertIsNone(state["function"])
        self.assertNotIn(MIGRATION_0038.name, state["ledger"])
        self._assert_production_at_rest(state, baseline["app_acl"])

    def test_c_to_j_committed_0038_through_the_actual_runner(self):
        text = MIGRATION_0038.read_text(encoding="utf-8")
        database = self._fresh_runner_database()
        baseline = self._state(database)

        # C. the committed migration applies through the runner's real elevate/apply/release path
        error = self._run_with_runner(database, self._migrations_through_0038(text), elevate=True)
        self.assertIsNone(error, f"0038 failed under the production permission model: {error!r}")
        state = self._state(database)
        self.assertEqual(state["ledger"].get(MIGRATION_0038.name), _checksum(text))
        self.assertRegex(state["ledger"][MIGRATION_0038.name], r"^[0-9a-f]{16}$")

        owner, secdef, config, acl, result, arguments = state["function"]
        self.assertEqual(arguments, "", "signature changed")
        self.assertEqual(result, "TABLE(tenant_id uuid, role text)", "signature changed")
        # D. ownership
        self.assertEqual(owner, PROVISIONER)
        # E. SECURITY DEFINER
        self.assertTrue(secdef)
        # F. pinned search_path unchanged
        self.assertEqual(config, EXPECTED_SEARCH_PATH)
        # G. EXECUTE privileges: app_backend only
        self.assertEqual(
            acl, f"{{{PROVISIONER}=X/{PROVISIONER},app_backend=X/{PROVISIONER}}}"
        )
        with self._connect(BOOTSTRAP, database) as conn:
            for role, allowed in (("app_backend", True), ("authenticated", False),
                                  ("anon", False), ("public", False), ("service_role", False)):
                with self.subTest(execute_role=role):
                    self.assertEqual(
                        conn.execute(
                            "select has_function_privilege(%s, 'app.my_active_tenants()', 'EXECUTE')",
                            (role,),
                        ).fetchone()[0],
                        allowed,
                    )
        # H + I + J. CREATE revoked, membership back to production, no superuser/escalation
        self._assert_production_at_rest(state, baseline["app_acl"])
        self.assertEqual(state["operator_owned_definers"], DOCUMENTED_OPERATOR_OWNED_DEFINERS)
        self._assert_isolation_behaviour(database)

    def _assert_isolation_behaviour(self, database: str):
        """The definer returns only the caller's active memberships; the table stays RLS-filtered."""
        import psycopg  # noqa: PLC0415

        user, other = "aaaaaaaa-0038-4000-8000-000000000001", "bbbbbbbb-0038-4000-8000-000000000001"
        tenants = [f"1{n}000000-0038-4000-8000-000000000000" for n in range(3)]
        with self._connect(BOOTSTRAP, database) as conn:
            try:
                for index, tenant in enumerate(tenants):
                    conn.execute(
                        "insert into public.tenants (id, name, slug, status) values (%s, %s, %s, 'active')",
                        (tenant, f"T{index}", f"t0038-{index}"),
                    )
                for tenant, member, role, status in (
                    (tenants[0], user, "owner", "active"), (tenants[1], user, "staff", "active"),
                    (tenants[2], user, "staff", "invited"), (tenants[0], other, "staff", "active"),
                ):
                    conn.execute(
                        "insert into public.tenant_users (tenant_id, user_id, role, status)"
                        " values (%s, %s, %s, %s)", (tenant, member, role, status),
                    )

                def as_backend(claims_sub, sql):
                    with conn.transaction():
                        conn.execute("set local role app_backend")
                        claims = json.dumps({"sub": claims_sub}) if claims_sub else ""
                        conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
                        return sorted((str(t), r) for t, r in conn.execute(sql).fetchall())

                mine = "select tenant_id, role from app.my_active_tenants()"
                self.assertEqual(
                    as_backend(user, mine), sorted([(tenants[0], "owner"), (tenants[1], "staff")])
                )
                self.assertEqual(as_backend(other, mine), [(tenants[0], "staff")])
                self.assertEqual(as_backend(None, mine), [], "unauthenticated must see nothing")
                self.assertEqual(
                    as_backend(user, "select tenant_id, role from public.tenant_users"), [],
                    "app_backend without tenant context must not read tenant_users directly",
                )
                for role in ("authenticated", "anon"):
                    with self.subTest(denied_role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with conn.transaction():
                            conn.execute(f"set local role {role}")
                            conn.execute(mine)
            finally:
                conn.rollback()

    def _exposed_role_violations(self, dbname: str) -> list:
        """Privileges the Supabase Data API roles must never hold on this platform's objects.

        anon and service_role: nothing on any public table or sequence. authenticated: nothing
        on a backend-only table, and never TRUNCATE/TRIGGER/REFERENCES (which bypass RLS).
        """
        try:
            from tests.saas_test_support import AUTHENTICATED_INACCESSIBLE_TABLES  # noqa: PLC0415
        except ImportError:  # pragma: no cover
            from saas_test_support import AUTHENTICATED_INACCESSIBLE_TABLES  # type: ignore  # noqa: PLC0415
        with self._connect(BOOTSTRAP, dbname) as conn:
            rows = conn.execute(
                "select r.rolname, c.relname, c.relkind::text, p.priv from pg_class c"
                " cross join (values ('anon'), ('authenticated'), ('service_role')) r(rolname)"
                " cross join unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
                "   'REFERENCES','TRIGGER']) p(priv)"
                " where c.relnamespace = 'public'::regnamespace and c.relkind in ('r', 'S')"
                "   and (c.relkind = 'r' or p.priv in ('SELECT', 'UPDATE'))"
                "   and has_table_privilege(r.rolname, c.oid, p.priv)"
            ).fetchall()
        violations = []
        for role, name, kind, priv in rows:
            if role in ("anon", "service_role"):
                violations.append((role, name, priv))
            elif kind == "S" and name.split("_id_seq")[0] in AUTHENTICATED_INACCESSIBLE_TABLES:
                violations.append((role, name, priv))
            elif name in AUTHENTICATED_INACCESSIBLE_TABLES or name == "saas_schema_migrations":
                violations.append((role, name, priv))
            elif priv in ("TRUNCATE", "TRIGGER", "REFERENCES"):
                violations.append((role, name, priv))
        return sorted(violations)

    def test_supabase_default_privileges_reproduce_the_production_leak_until_0041(self):
        """0001-0037 under Supabase defaults leave orders exposed (as found in production)."""
        leaked = self._exposed_role_violations(PRE_0038_TEMPLATE)
        self.assertIn(("anon", "orders", "TRUNCATE"), leaked)
        self.assertIn(("service_role", "order_items_id_seq", "UPDATE"), leaked)
        database = self._fresh_database(self.BUNDLE_DATABASE)
        self.assertIsNone(
            self._execute_script("postgres", database, build_bundle(self.LAST_PRODUCTION_VERSION)))
        self.assertEqual(self._exposed_role_violations(database), [])

    def test_full_chain_0001_to_latest_via_run_migrations_main_as_non_superuser(self):
        """Fresh empty database, every migration file, the runner's CLI entry point."""
        database = self._fresh_runner_database(from_template=False)
        # Schema app does not exist yet here; its ACL must end up as it was after 0037.
        template_acl = self._state(PRE_0038_TEMPLATE)["app_acl"]

        exit_code = runner.main(
            ["--database-url", f"postgresql://postgres@{HOST}:{self.port}/{database}"]
        )

        self.assertEqual(exit_code, 0)
        state = self._state(database)
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        self.assertEqual(
            state["ledger"],
            {path.name: _checksum(path.read_text(encoding="utf-8")) for path in files},
        )
        self.assertEqual(state["function"][0], PROVISIONER)
        self.assertEqual(state["operator_owned_definers"], DOCUMENTED_OPERATOR_OWNED_DEFINERS)
        self._assert_production_at_rest(state, template_acl)
        self.assertEqual(self._exposed_role_violations(database), [])


    # ------------------------------------------------------- production bundle and reset
    BUNDLE_DATABASE = "saas_bundle"
    LAST_PRODUCTION_VERSION = "0037_quotation_briefs_and_deliveries.sql"
    PLATFORM_TABLES = ("plans", "platform_admins", "reserved_phone_numbers", "saas_schema_migrations")

    def _fresh_database(self, name: str) -> str:
        with self._connect(BOOTSTRAP, "postgres", autocommit=True) as conn:
            conn.execute(f"drop database if exists {name} with (force)")
            conn.execute(f"create database {name} template {PRE_0038_TEMPLATE} owner postgres")
        return name

    def _execute_script(self, user: str, dbname: str, sql: str):
        """Run a whole script (with its own begin/commit) the way the SQL editor does."""
        import psycopg  # noqa: PLC0415

        with self._connect(user, dbname, autocommit=True) as conn:
            cursor = psycopg.ClientCursor(conn)
            try:
                cursor.execute(sql)
                return None
            except psycopg.Error as error:
                return error
            finally:
                cursor.close()

    def _snapshot(self, dbname: str) -> dict:
        with self._connect(BOOTSTRAP, dbname) as conn:
            def q(sql):
                return sorted(tuple(map(str, row)) for row in conn.execute(sql).fetchall())
            return {
                "functions": q(
                    "select p.oid::regprocedure::text, pg_get_userbyid(p.proowner), p.prosecdef,"
                    " p.proconfig, p.proacl, md5(p.prosrc) from pg_proc p"
                    " where p.pronamespace in ('app'::regnamespace, 'public'::regnamespace)"
                ),
                "relations": q(
                    "select c.oid::regclass::text, c.relkind, pg_get_userbyid(c.relowner), c.relacl,"
                    " c.relrowsecurity, c.relforcerowsecurity from pg_class c"
                    " where c.relnamespace in ('app'::regnamespace, 'public'::regnamespace)"
                ),
                "policies": q(
                    "select polrelid::regclass::text, polname, polcmd, polroles::regrole[]::text,"
                    " pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) from pg_policy"
                ),
                "schemas": q("select nspname, nspacl from pg_namespace where nspname in ('app', 'public')"),
                "ledger": q("select version, checksum from public.saas_schema_migrations"),
                "memberships": q(
                    "select pg_get_userbyid(member), pg_get_userbyid(roleid), pg_get_userbyid(grantor),"
                    " admin_option, inherit_option, set_option from pg_auth_members"
                ),
            }

    def test_bundle_produces_exactly_the_runner_result_as_a_non_superuser(self):
        runner_db = self._fresh_runner_database()
        self.assertIsNone(self._run_with_runner(runner_db, MIGRATIONS_DIR, elevate=True))
        bundle_db = self._fresh_database(self.BUNDLE_DATABASE)
        baseline_acl = self._state(bundle_db)["app_acl"]

        error = self._execute_script("postgres", bundle_db, build_bundle(self.LAST_PRODUCTION_VERSION))

        self.assertIsNone(error, repr(error))
        self.assertEqual(self._snapshot(bundle_db), self._snapshot(runner_db))
        state = self._state(bundle_db)
        self._assert_production_at_rest(state, baseline_acl)
        self.assertEqual(state["function"][0], PROVISIONER)
        self.assertEqual(state["operator_owned_definers"], DOCUMENTED_OPERATOR_OWNED_DEFINERS)

    def test_bundle_refuses_a_ledger_that_is_not_exactly_the_expected_one(self):
        database = self._fresh_database(self.BUNDLE_DATABASE)
        bundle = build_bundle(self.LAST_PRODUCTION_VERSION)
        self.assertIsNone(self._execute_script("postgres", database, bundle))
        before = self._snapshot(database)

        error = self._execute_script("postgres", database, bundle)  # already applied

        self.assertIsNotNone(error)
        self.assertIn("ledger precondition failed", str(error))
        self.assertEqual(self._snapshot(database), before)

    def test_bundle_is_all_or_nothing(self):
        database = self._fresh_database(self.BUNDLE_DATABASE)
        with self._connect(BOOTSTRAP, database, autocommit=True) as conn:
            # An incompatible pre-existing function makes 0038's CREATE OR REPLACE fail midway.
            conn.execute("create function app.my_active_tenants() returns int language sql as 'select 1'")
        before = self._snapshot(database)
        baseline_acl = self._state(database)["app_acl"]

        error = self._execute_script("postgres", database, build_bundle(self.LAST_PRODUCTION_VERSION))

        self.assertIsNotNone(error)
        self.assertEqual(self._snapshot(database), before)
        state = self._state(database)
        self.assertEqual(state["membership"], PRODUCTION_MEMBERSHIP)
        self.assertFalse(state["provisioner_create_on_app"])
        self.assertEqual(state["app_acl"], baseline_acl)

    def _seed(self, database: str) -> None:
        import psycopg  # noqa: PLC0415

        with self._connect(BOOTSTRAP, database) as conn:
            conn.execute("select set_config('saas.allow_dev_data', 'on', false)")
            for path in sorted((REPO / "db" / "seeds").glob("*.sql")):
                cursor = psycopg.ClientCursor(conn)
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.close()
            conn.execute(
                "insert into public.reserved_phone_numbers (phone_number_id, reason)"
                " values ('reserved-0038', 'test')"
            )
            conn.commit()

    def _counts(self, database: str, schema: str = "public") -> dict:
        with self._connect(BOOTSTRAP, database) as conn:
            tables = [r[0] for r in conn.execute(
                "select relname from pg_class where relnamespace = %s::regnamespace"
                " and relkind = 'r' and left(relname, 1) <> '_'", (schema,)
            )]
            return {t: conn.execute(f'select count(*) from {schema}."{t}"').fetchone()[0] for t in tables}

    def test_tenant_reset_removes_every_tenant_row_and_keeps_the_platform(self):
        database = self._fresh_database(self.BUNDLE_DATABASE)
        self.assertIsNone(
            self._execute_script("postgres", database, build_bundle(self.LAST_PRODUCTION_VERSION)))
        self._seed(database)
        before = self._counts(database)
        tenants = before["tenants"]
        self.assertGreater(tenants, 0)
        self.assertGreater(before["product_variants"], 0)
        self.assertGreater(before["tenant_users"], 0)
        with self._connect(BOOTSTRAP, database) as conn:
            attributed = conn.execute(
                "select count(*) from public.audit_log where tenant_id is not null").fetchone()[0]
        schema = "tenant_reset_backup_test"

        # Wrong expectation: refused, nothing changes.
        error = self._execute_script("postgres", database, build_reset(tenants + 1, schema))
        self.assertIn(f"expected {tenants + 1} tenant(s)", str(error))
        self.assertEqual(self._counts(database), before)

        error = self._execute_script("postgres", database, build_reset(tenants, schema))

        self.assertIsNone(error, repr(error))
        after = self._counts(database)
        backup = self._counts(database, schema)
        for table, count in before.items():
            with self.subTest(table=table):
                if table in self.PLATFORM_TABLES:
                    self.assertEqual(after[table], count, "platform data changed")
                elif table == "audit_log":
                    self.assertEqual(after[table], count + 1, "audit history must be kept (+1 reset entry)")
                else:
                    self.assertEqual(after[table], 0, "tenant data remains")
                    self.assertEqual(backup[table], count, "removed rows were not backed up")
        with self._connect(BOOTSTRAP, database) as conn:
            self.assertEqual(
                conn.execute(f"select count(*) from {schema}._audit_tenant_links").fetchone()[0], attributed)
            self.assertEqual(conn.execute(
                "select count(*) from public.audit_log where action = 'platform.tenant_reset'"
            ).fetchone()[0], 1)
            for role in ("anon", "authenticated", "app_backend", "service_role", PROVISIONER):
                with self.subTest(backup_access=role):
                    self.assertFalse(conn.execute(
                        "select has_schema_privilege(%s, %s, 'USAGE')", (role, schema)).fetchone()[0])
        state = self._state(database)
        self._assert_production_at_rest(state, state["app_acl"])

        # Re-running is refused because the backup schema already exists.
        self.assertIn("already exists",
                      str(self._execute_script("postgres", database, build_reset(0, schema))))


if __name__ == "__main__":
    unittest.main()
