"""The engine must distinguish a verified customer identity from its staff fallbacks.

``BotResponseEngine.respond`` falls back to the tenant's own WhatsApp number (and finally
the tenant id) when no ``customer_key`` is supplied. Those fallbacks keep drafts and
handovers working for staff, but they are NOT a customer identity and must never be used
to authorise access to a customer's order.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from saas import bot as bot_module
from saas import tools as tool_module
from saas.bot import BotResponseEngine, TenantBotContext


class _Provider:
    def generate(self, request):  # noqa: ANN001 - protocol-shaped fake
        return "ok"


def _context(whatsapp_wa_id="254700000001"):
    return TenantBotContext(
        tenant={"id": "tenant-1", "name": "Test", "whatsapp_wa_id": whatsapp_wa_id},
        settings={},
        catalogue=[],
    )


class ToolContextIdentityTests(unittest.TestCase):
    def _respond_and_capture(self, **kwargs):
        captured = {}
        real_context = tool_module.ToolContext

        def spy(**context_kwargs):
            captured.update(context_kwargs)
            return real_context(**context_kwargs)

        engine = BotResponseEngine(None, _Provider())
        with patch.object(BotResponseEngine, "_system_prompt", return_value="system"), patch(
            "saas.bot._tools.ToolContext", side_effect=spy
        ):
            engine.respond(
                "tenant-1",
                "hello",
                (),
                channel="whatsapp",
                safe_test_mode=False,
                context=_context(),
                **kwargs,
            )
        return captured

    def test_a_supplied_customer_key_is_a_verified_identity(self):
        captured = self._respond_and_capture(customer_key="254700000001")
        self.assertEqual(captured["customer_wa_id"], "254700000001")
        self.assertEqual(captured["customer_key"], "254700000001")

    def test_the_tenant_fallback_is_never_a_verified_identity(self):
        captured = self._respond_and_capture()
        self.assertIsNone(
            captured["customer_wa_id"],
            "the tenant's own number must not authorise a customer's order lookup",
        )
        self.assertEqual(captured["customer_key"], "254700000001")

    def test_staff_flag_is_propagated(self):
        captured = self._respond_and_capture(is_staff=True)
        self.assertTrue(captured["is_staff"])
        self.assertIsNone(captured["customer_wa_id"])


if __name__ == "__main__":
    unittest.main()
