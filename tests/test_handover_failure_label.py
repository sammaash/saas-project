"""The handover failure reason must not change because an exception gained a subclass."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from platform_admin.config import Settings
from platform_admin.services import handover as handover_service
from platform_admin.services import whatsapp as whatsapp_service

PHONE_ID = "phone-test-1"


class HandoverFailureLabelTests(unittest.TestCase):
    def _notify(self, **patch_kwargs):
        settings = Settings(secret_key="s" * 40, whatsapp_send_enabled=True)
        with patch(
            "platform_admin.services.handover.whatsapp_service.send_text",
            **patch_kwargs,
        ):
            return handover_service._notify(
                settings,
                channel={"credentials_ref": "tenants/tenant-1/whatsapp"},
                phone_number_id=PHONE_ID,
                tenant_id="tenant-1",
                target="254700000001",
                text="A customer needs help",
            )

    def test_a_send_failure_keeps_the_existing_label(self):
        reason = self._notify(
            side_effect=whatsapp_service.WhatsAppSendError(
                "WhatsApp message delivery failed.", 502, provider_code="131047"
            )
        )
        self.assertTrue(reason.startswith("ServiceError: "), reason)
        self.assertIn("WhatsApp message delivery failed.", reason)

    def test_a_non_service_error_keeps_its_own_name(self):
        reason = self._notify(side_effect=RuntimeError("boom"))
        self.assertTrue(reason.startswith("RuntimeError: "), reason)

    def test_a_successful_send_returns_no_reason(self):
        self.assertIsNone(self._notify(return_value="wamid.staff-1"))


if __name__ == "__main__":
    unittest.main()
