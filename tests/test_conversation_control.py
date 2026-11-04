"""Unit tests for the per-conversation AI control state machine and its audit."""

from __future__ import annotations

import contextlib
import unittest
import uuid
from unittest.mock import patch

from platform_admin.config import Settings
from platform_admin.services import conversation_control
from platform_admin.services.validation import ServiceError


def _settings():
    return Settings(secret_key="s" * 40, cookie_secure=False)


class _Column:
    def __init__(self, name):
        self.name = name


class _StubCursor:
    def __init__(self, results=()):
        self._results = list(results)
        self.executed = []
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = (
            self._results.pop(0)
            if self._results
            else {"columns": ("ok",), "rows": [("ok",)]}
        )
        if isinstance(result, Exception):
            raise result
        self.description = [_Column(name) for name in result["columns"]]
        self._rows = list(result["rows"])

    def fetchone(self):
        return self._rows[0] if self._rows else None


@contextlib.contextmanager
def _txn(cursor):
    yield cursor


class ControlUnitTests(unittest.TestCase):
    def _set_mode(self, cursor, new_mode, actor_user_id="user-1"):
        cursor._results.insert(0, {"columns": ("ok",), "rows": [("ok",)]})
        with patch(
            "platform_admin.services.conversation_control.tenant_transaction",
            lambda *args, **kwargs: _txn(cursor),
        ):
            return conversation_control.set_mode(
                _settings(),
                actor_user_id=actor_user_id,
                tenant_id=str(uuid.uuid4()),
                conversation_id=str(uuid.uuid4()),
                new_mode=new_mode,
            )

    def _set_attention(self, cursor, active, actor_user_id="user-1"):
        cursor._results.insert(0, {"columns": ("ok",), "rows": [("ok",)]})
        with patch(
            "platform_admin.services.conversation_control.tenant_transaction",
            lambda *args, **kwargs: _txn(cursor),
        ):
            return conversation_control.set_needs_attention(
                _settings(),
                actor_user_id=actor_user_id,
                tenant_id=str(uuid.uuid4()),
                conversation_id=str(uuid.uuid4()),
                active=active,
            )

    def test_every_allowed_transition_records_its_event(self):
        cases = {
            ("ai_active", "paused"): "ai_paused",
            ("ai_active", "human"): "human_takeover",
            ("paused", "human"): "human_takeover",
            ("paused", "ai_active"): "ai_resumed",
            ("human", "ai_active"): "ai_resumed",
        }
        for (previous, new), event in cases.items():
            with self.subTest(transition=f"{previous}->{new}"):
                cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [(previous,)]}])
                result = self._set_mode(cursor, new)
                self.assertEqual(result, "changed")
                inserts = [p for sql, p in cursor.executed if "insert" in sql]
                self.assertEqual(len(inserts), 1)
                self.assertIn(event, str(inserts[0]))
                self.assertIn(previous, str(inserts[0]))

    def test_same_mode_is_an_idempotent_noop_without_event(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("paused",)]}])
        result = self._set_mode(cursor, "paused")
        self.assertEqual(result, "unchanged")
        self.assertFalse(any("insert" in sql for sql, _ in cursor.executed))
        self.assertFalse(
            any("update public.conversations set ai_mode" in sql for sql, _ in cursor.executed)
        )

    def test_human_to_paused_is_rejected(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("human",)]}])
        with self.assertRaises(ServiceError) as raised:
            self._set_mode(cursor, "paused")
        self.assertEqual(raised.exception.status, 409)

    def test_unknown_mode_is_rejected_before_any_query(self):
        with self.assertRaises(ServiceError) as raised:
            self._set_mode(_StubCursor(), "manual")
        self.assertEqual(raised.exception.status, 400)

    def test_missing_conversation_is_404(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": []}])
        with self.assertRaises(ServiceError) as raised:
            self._set_mode(cursor, "paused")
        self.assertEqual(raised.exception.status, 404)

    def test_lock_contention_is_409_and_writes_nothing(self):
        error = RuntimeError("busy")
        error.sqlstate = "55P03"  # noqa: B010
        cursor = _StubCursor([error])
        with self.assertRaises(ServiceError) as raised:
            self._set_mode(cursor, "paused")
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(any("insert" in sql for sql, _ in cursor.executed))

    def test_user_events_carry_the_actor_system_events_do_not(self):
        actor = str(uuid.uuid4())
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("ai_active",)]}])
        self._set_mode(cursor, "paused", actor_user_id=actor)
        params = [p for sql, p in cursor.executed if "insert" in sql][0]
        self.assertIn(actor, str(params))
        self.assertIn("'user'", str(params).replace('"', "'"))

        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("ai_active",)]}])
        self._set_mode(cursor, "paused", actor_user_id=None)
        params = [p for sql, p in cursor.executed if "insert" in sql][0]
        self.assertIn("None", str(params))  # NULL actor, system event

    def test_needs_attention_toggle_and_idempotency(self):
        cursor = _StubCursor([{"columns": ("needs_attention",), "rows": [(False,)]}])
        self.assertEqual(self._set_attention(cursor, True), "changed")
        inserts = [p for sql, p in cursor.executed if "insert" in sql]
        self.assertIn("needs_attention_set", str(inserts))

        cursor = _StubCursor([{"columns": ("needs_attention",), "rows": [(True,)]}])
        self.assertEqual(self._set_attention(cursor, True), "unchanged")
        self.assertFalse(any("insert" in sql for sql, _ in cursor.executed))

        cursor = _StubCursor([{"columns": ("needs_attention",), "rows": [(True,)]}])
        self.assertEqual(self._set_attention(cursor, False), "changed")
        inserts = [p for sql, p in cursor.executed if "insert" in sql]
        self.assertIn("needs_attention_cleared", str(inserts))


class Migration0039Tests(unittest.TestCase):
    """Static review of migration 0039 (local file; never applied by these tests)."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        cls.sql = (
            Path(__file__).resolve().parent.parent
            / "db"
            / "migrations"
            / "0039_conversation_ai_control.sql"
        ).read_text(encoding="utf-8")
        cls.lower = cls.sql.lower()

    def test_ai_mode_defaults_to_ai_active_and_validates_values(self):
        self.assertIn("default 'ai_active'", self.lower)
        self.assertIn("check (ai_mode in ('ai_active', 'paused', 'human'))", self.lower)
        self.assertIn("needs_attention boolean not null default false", self.lower)

    def test_grants_go_only_to_app_backend(self):
        self.assertIn("grant select, insert on public.conversation_control_events to app_backend;", self.lower)
        self.assertIn("revoke all on public.conversation_control_events from public;", self.lower)
        self.assertNotIn("grant", self.lower.replace("grant select, insert on public.conversation_control_events to app_backend;", ""))

    def test_rls_uses_the_tenant_context_function(self):
        self.assertIn("enable row level security", self.lower)
        self.assertIn("tenant_id = app.current_tenant_id()", self.lower)
        self.assertNotIn("app.tenant_id)", self.lower.replace("app.current_tenant_id()", ""))

    def test_actor_semantics_are_enforced_by_constraints(self):
        self.assertIn("actor_user_id   uuid,", self.lower)
        self.assertIn("check ((actor_type = 'system') = (actor_user_id is null))", self.lower)
        self.assertIn("check ((event_type <> 'ai_reply_suppressed') or (actor_type = 'system'))", self.lower)

    def test_audit_table_uses_the_composite_foreign_key(self):
        self.assertIn(
            "foreign key (conversation_id, tenant_id)", self.lower
        )
        self.assertIn("references public.conversations (id, tenant_id)", self.lower)


class StartupSchemaGuardTests(unittest.TestCase):
    """The startup schema guard must refuse databases without migration 0039."""

    def test_required_schema_names_the_conversation_control_objects(self):
        from platform_admin.config import REQUIRED_SCHEMA

        required = dict(REQUIRED_SCHEMA)
        self.assertIn("ai_mode", required["public.conversations"])
        self.assertIn("needs_attention", required["public.conversations"])
        self.assertIn("public.conversation_control_events", required)

    def test_missing_schema_objects_reports_a_pre_0039_database(self):
        from platform_admin.config import missing_schema_objects

        class _SchemaCursor:
            def __init__(self):
                self._tables = {"public.conversations"}
                self._columns = {("public.conversations", "id")}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                self._current = sql
                self._params = params

            def fetchone(self):
                name = self._params[0]
                return (name,) if name in self._tables else (None,)

            def fetchall(self):
                table = self._params[0]
                return [(col,) for (t, col) in self._columns if t == table]

        class _SchemaConnection:
            def cursor(self):
                return _SchemaCursor()

        missing = missing_schema_objects(_SchemaConnection())
        self.assertIn("public.conversations.ai_mode (column)", missing)
        self.assertIn("public.conversations.needs_attention (column)", missing)
        self.assertIn("public.conversation_control_events (table)", missing)


if __name__ == "__main__":
    unittest.main()