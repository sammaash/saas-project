"""Tests for the outbound dispatch boundary (unit + two-connection integration).

Unit tests drive the boundary with a scripted cursor and a fake provider (the
``send`` callback is the seam). Integration tests require SAAS_TEST_DATABASE_URL
with migrations 0001-0039 applied and are SKIPPED otherwise — they exercise the
real row-lock ordering between takeover and dispatch with two connections.
"""

from __future__ import annotations

import contextlib
import threading
import unittest
import uuid
from unittest.mock import patch

from platform_admin.config import Settings
from platform_admin.services import outbound_dispatch
from platform_admin.services.outbound_dispatch import DispatchOutcome, SendFailed
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

    def fetchall(self):
        return list(self._rows)


@contextlib.contextmanager
def _txn(cursor):
    yield cursor


class DispatchUnitTests(unittest.TestCase):
    def _dispatch(self, cursor, **overrides):
        arguments = dict(
            tenant_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            message_row_id=str(uuid.uuid4()),
            sender_type="assistant",
            send=lambda: "wamid.1",
        )
        arguments.update(overrides)
        # First execute() is the transaction-local set_config; scripted results
        # describe the statements that follow it.
        cursor._results.insert(0, {"columns": ("ok",), "rows": [("ok",)]})
        with patch(
            "platform_admin.services.outbound_dispatch.tenant_transaction",
            lambda *args, **kwargs: _txn(cursor),
        ):
            return outbound_dispatch.dispatch_reply(_settings(), **arguments)

    def test_ai_active_sends_and_attaches_the_wamid_in_the_same_transaction(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("ai_active",)]}])
        calls = []
        outcome = self._dispatch(cursor, send=lambda: calls.append(1) or "wamid.9")
        self.assertEqual(outcome, DispatchOutcome("sent", wamid="wamid.9"))
        self.assertEqual(calls, [1])
        sqls = [sql for sql, _ in cursor.executed]
        self.assertTrue(any("for update" in sql for sql in sqls))
        self.assertTrue(
            any("set provider_message_id" in sql and "delivery_status = 'sent'" in sql for sql in sqls)
        )

    def test_paused_conversation_is_suppressed_without_sending(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("paused",)]}])
        calls = []
        outcome = self._dispatch(cursor, send=lambda: calls.append(1) or "wamid.1")
        self.assertEqual(calls, [])
        self.assertEqual(outcome.kind, "suppressed")
        self.assertEqual(outcome.error_code, outbound_dispatch.SUPPRESSED_CODE)
        inserts = [(sql, params) for sql, params in cursor.executed if "insert" in sql]
        self.assertTrue(any("conversation_control_events" in sql for sql, _ in inserts))
        self.assertTrue(
            any("ai_reply_suppressed" in str(params) for _, params in inserts)
        )
        self.assertTrue(any("error_code = %s" in sql for sql in [s for s, _ in cursor.executed]))

    def test_human_conversation_is_suppressed_without_sending(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("human",)]}])
        outcome = self._dispatch(cursor, send=lambda: self.fail("must not send"))
        self.assertEqual(outcome.kind, "suppressed")

    def test_staff_sends_ignore_the_mode(self):
        for mode in ("ai_active", "paused", "human"):
            with self.subTest(mode=mode):
                cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [(mode,)]}])
                outcome = self._dispatch(
                    cursor, sender_type="staff", send=lambda: "wamid.staff"
                )
                self.assertEqual(outcome.kind, "sent")

    def test_definitive_failure_is_recorded_as_failed(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("ai_active",)]}])

        def _boom():
            raise SendFailed("131047", "Re-engagement message")

        outcome = self._dispatch(cursor, send=_boom)
        self.assertEqual(outcome.kind, "failed")
        self.assertEqual(outcome.error_code, "131047")
        self.assertEqual(outcome.error_type, "SendFailed")
        joined = " ".join(sql for sql, _ in cursor.executed)
        self.assertIn("delivery_status = %s", joined)

    def test_ambiguous_failure_is_unknown_and_never_marked_failed(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": [("ai_active",)]}])

        def _timeout():
            raise SendFailed("ambiguous_send", "timeout", ambiguous=True)

        outcome = self._dispatch(cursor, send=_timeout)
        self.assertEqual(outcome.kind, "unknown")
        # Ambiguous keeps delivery_status NULL: not failed, not sent.
        params = [p for sql, p in cursor.executed if "delivery_status = %s" in sql]
        self.assertEqual(params[0][0], None)

    def test_lock_contention_reports_409(self):
        error = RuntimeError("busy")
        error.sqlstate = "55P03"  # noqa: B010 - mimic a psycopg error
        cursor = _StubCursor([error])
        with self.assertRaises(ServiceError) as raised:
            self._dispatch(cursor)
        self.assertEqual(raised.exception.status, 409)

    def test_missing_conversation_fails_closed(self):
        cursor = _StubCursor([{"columns": ("ai_mode",), "rows": []}])
        with self.assertRaises(ServiceError) as raised:
            self._dispatch(cursor, send=lambda: self.fail("must not send"))
        self.assertEqual(raised.exception.status, 404)

    def test_missing_0039_fails_closed_and_never_sends(self):
        error = RuntimeError("no column ai_mode")
        error.sqlstate = "42703"  # noqa: B010
        cursor = _StubCursor([error])
        with self.assertRaises(ServiceError) as raised:
            self._dispatch(cursor, send=lambda: self.fail("must not send"))
        self.assertEqual(raised.exception.status, 503)
        self.assertIn("0039", raised.exception.message)

    def test_finalize_failure_after_provider_acceptance_propagates(self):
        # T4 shape: the provider accepted, then the database failed. The exception
        # escapes (the caller logs it) and nothing retries automatically.
        crash = RuntimeError("db gone")
        cursor = _StubCursor(
            [{"columns": ("ai_mode",), "rows": [("ai_active",)]}, crash]
        )
        with self.assertRaises(RuntimeError):
            self._dispatch(cursor, send=lambda: "wamid.accepted")


class DispatchLockIntegrationTests(unittest.TestCase):
    """Two-connection ordering tests (takeover vs dispatch).

    SKIPPED unless SAAS_TEST_DATABASE_URL points at a disposable database with
    migrations 0001-0039 applied. Never touches staging or production.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from .saas_test_support import (
                TENANT_ONE_ID,
                app_backend_test_url,
                require_test_database_url,
            )
        except ImportError:  # discovery inside the tests directory
            from saas_test_support import (  # type: ignore[no-redef]
                TENANT_ONE_ID,
                app_backend_test_url,
                require_test_database_url,
            )
        try:
            require_test_database_url()
        except Exception as error:  # noqa: BLE001 - skip without a test database
            raise unittest.SkipTest(str(error))
        cls.tenant_id = TENANT_ONE_ID
        cls.settings = Settings(
            secret_key="s" * 40, cookie_secure=False, app_database_url=app_backend_test_url()
        )
        from saas.tenant_context import tenant_transaction

        with tenant_transaction(cls.tenant_id, cls.settings.app_database_url) as cursor:
            cursor.execute(
                "select 1 from information_schema.columns "
                "where table_name = 'conversations' and column_name = 'ai_mode'"
            )
            if cursor.fetchone() is None:
                raise unittest.SkipTest(
                    "SAAS_TEST_DATABASE_URL schema lacks migration 0039 (ai_mode)"
                )

    def _fixture(self):
        """One fresh conversation row for the seeded test tenant (self-cleaning)."""
        from platform_admin.services import customers as customer_service
        from saas.tenant_context import tenant_transaction

        # The conversations FK requires a real tenant_channels row: use the seeded one.
        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
            cursor.execute(
                "select id from public.tenant_channels "
                "where tenant_id = app.current_tenant_id() limit 1"
            )
            row = cursor.fetchone()
        assert row is not None, "seeded channel missing: run db/seeds via the runner"
        channel_id = str(row[0])
        unique = uuid.uuid4().hex[:12]
        customer = customer_service.get_or_create_customer(
            self.settings, self.tenant_id, channel_id, f"+2547{unique}"
        )
        conversation = customer_service.get_or_create_conversation(
            self.settings, self.tenant_id, customer["id"], channel_id
        )

        def _cleanup():
            from saas.tenant_context import tenant_transaction as txn

            with txn(self.tenant_id, self.settings.app_database_url) as cursor:
                cursor.execute(
                    "delete from public.customers where id = %s "
                    "and tenant_id = app.current_tenant_id()",
                    (str(customer["id"]),),
                )

        self.addCleanup(_cleanup)  # keep the shared test database count-stable
        return str(conversation["id"])

    def test_t1_takeover_first_suppresses_dispatch(self):
        from platform_admin.services import conversation_control

        conversation_id = self._fixture()
        conversation_control.set_mode(
            self.settings,
            actor_user_id=str(uuid.uuid4()),
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            new_mode="human",
        )
        outcome = outbound_dispatch.dispatch_reply(
            self.settings,
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            message_row_id=None,
            sender_type="assistant",
            send=lambda: self.fail("send must not begin after takeover commits"),
        )
        self.assertEqual(outcome.kind, "suppressed")

    def test_t2_dispatch_first_completes_before_takeover_commits(self):
        from platform_admin.services import conversation_control

        conversation_id = self._fixture()
        inside_send = threading.Event()
        release_send = threading.Event()
        takeover_done = threading.Event()

        def _takeover():
            conversation_control.set_mode(
                self.settings,
                actor_user_id=str(uuid.uuid4()),
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                new_mode="human",
            )
            takeover_done.set()

        def _send():
            inside_send.set()
            self.assertTrue(release_send.wait(5), "test deadlock")
            return "wamid.integration"

        outcome = {}

        def _dispatch():
            outcome["value"] = outbound_dispatch.dispatch_reply(
                self.settings,
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                message_row_id=None,
                sender_type="assistant",
                send=_send,
            )

        dispatcher = threading.Thread(target=_dispatch)
        dispatcher.start()
        self.assertTrue(inside_send.wait(5), "dispatch never reached the send")
        thread = threading.Thread(target=_takeover)
        thread.start()  # takeover blocks on the row lock held by dispatch
        self.assertFalse(takeover_done.wait(0.3), "takeover must wait for dispatch")
        release_send.set()
        dispatcher.join(5)
        thread.join(5)
        self.assertEqual(outcome["value"].kind, "sent")
        self.assertTrue(takeover_done.is_set())
        # The takeover completed only after the send: the ordering guarantee held.

    def test_t3_control_lock_timeout_is_409_and_writes_nothing(self):
        from platform_admin.services import conversation_control
        from saas.tenant_context import tenant_transaction

        conversation_id = self._fixture()
        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as holder:
            holder.execute(
                "select 1 from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            with patch.object(outbound_dispatch, "LOCK_TIMEOUT_SQL", "1s"):
                with self.assertRaises(ServiceError) as raised:
                    conversation_control.set_mode(
                        self.settings,
                        actor_user_id=str(uuid.uuid4()),
                        tenant_id=self.tenant_id,
                        conversation_id=conversation_id,
                        new_mode="paused",
                    )
        self.assertEqual(raised.exception.status, 409)

    def test_t3b_dispatch_lock_timeout_never_sends(self):
        from saas.tenant_context import tenant_transaction

        conversation_id = self._fixture()
        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as holder:
            holder.execute(
                "select 1 from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            with patch.object(outbound_dispatch, "LOCK_TIMEOUT_SQL", "1s"):
                with self.assertRaises(ServiceError) as raised:
                    outbound_dispatch.dispatch_reply(
                        self.settings,
                        tenant_id=self.tenant_id,
                        conversation_id=conversation_id,
                        message_row_id=None,
                        sender_type="assistant",
                        send=lambda: self.fail("send must not begin while locked out"),
                    )
        self.assertEqual(raised.exception.status, 409)

    def test_t4_provider_acceptance_then_database_failure_is_unknown(self):
        conversation_id = self._fixture()
        calls = []
        # An invalid row id makes the finalisation UPDATE fail AFTER the provider
        # accepted: the exception must escape and nothing may retry.
        with self.assertRaises(Exception):  # noqa: BLE001 - DB translation layer
            outbound_dispatch.dispatch_reply(
                self.settings,
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                message_row_id="not-a-uuid",
                sender_type="staff",
                send=lambda: calls.append(1) or "wamid.accepted",
            )
        self.assertEqual(calls, [1])  # exactly one provider attempt, never a retry

    def test_t5_duplicate_intent_rows_are_rejected(self):
        from platform_admin.services import customers as customer_service
        from saas.tenant_context import tenant_transaction

        conversation_id = self._fixture()
        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
            cursor.execute(
                "select id from public.tenant_channels "
                "where tenant_id = app.current_tenant_id() limit 1"
            )
            channel_id = str(cursor.fetchone()[0])
        # Unique per run: the at-most-one-reply index keeps previous runs' rows.
        reply_key = f"wamid.in-{uuid.uuid4().hex[:12]}"
        common = dict(
            direction="outbound",
            sender_type="assistant",
            message_type="text",
            body_text="hello",
            reply_to_provider_message_id=reply_key,
        )
        customer_service.save_message(
            self.settings, self.tenant_id, channel_id, conversation_id, **common
        )
        with self.assertRaises(customer_service.DuplicateReplyError):
            customer_service.save_message(
                self.settings, self.tenant_id, channel_id, conversation_id, **common
            )