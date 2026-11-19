"""Separate-process probe for the Stage 2 restart test (not collected by pytest).

``python tests/stage2_restart_probe.py <phase> <wa_id> <message_id>`` runs ONE phase in a
fresh interpreter against SAAS_TEST_DATABASE_URL (disposable database only) and prints a
JSON result line. Each phase builds a brand-new application, so nothing survives between
phases except what is in the database - exactly like a process restart.

    customer  - deliver one customer message through the real webhook
    pause     - the seeded owner presses "Pause AI" through the real portal route
    inbox     - the seeded owner opens the conversation in the inbox

WhatsApp HTTP is faked (no network). The AI engine is faked and records whether it ran.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_admin.app import create_app  # noqa: E402
from platform_admin.config import Settings  # noqa: E402
from saas.tenant_context import tenant_transaction  # noqa: E402
from saas_test_support import (  # noqa: E402
    TENANT_ONE_ID,
    TENANT_ONE_OWNER_USER_ID,
    TENANT_ONE_PHONE_NUMBER_ID,
    app_backend_test_url,
)

ENGINE_CALLS: list[str] = []
SENDS: list[str] = []


class _Engine:
    def __init__(self, *args, **kwargs):
        pass

    def respond(self, *args, **kwargs):
        ENGINE_CALLS.append("respond")
        return SimpleNamespace(
            text="AI reply", quotation=None, quotation_failed=False, quotation_pdf=None,
            handover=None, handover_failed=False,
        )


def _send(*args, **kwargs):
    SENDS.append(kwargs.get("text", ""))
    return f"wamid.probe-{len(SENDS)}"


def _settings() -> Settings:
    return Settings(
        secret_key="s" * 40,
        cookie_secure=False,
        app_database_url=app_backend_test_url(),
        whatsapp_app_secret="app-secret",
        whatsapp_send_enabled=True,
    )


def _conversation_id(wa_id: str) -> str:
    with tenant_transaction(TENANT_ONE_ID, app_backend_test_url()) as cursor:
        cursor.execute(
            "select c.id from public.conversations c join public.customers cu "
            "on cu.id = c.customer_id where cu.wa_id = %s",
            (wa_id,),
        )
        return str(cursor.fetchone()[0])


def main(phase: str, wa_id: str, message_id: str) -> dict:
    app = create_app(_settings(), bot_provider=object())
    client = app.test_client()
    with patch("platform_admin.routes.BotResponseEngine", _Engine), patch(
        "platform_admin.services.whatsapp.send_text", side_effect=_send
    ):
        if phase == "customer":
            payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": TENANT_ONE_PHONE_NUMBER_ID},
                "messages": [{"id": message_id, "from": wa_id,
                              "text": {"body": "Do you have red velvet?"}}],
            }}]}]}
            body = json.dumps(payload).encode()
            signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
            response = client.post(
                "/webhooks/whatsapp", data=body,
                headers={"Content-Type": "application/json",
                         "X-Hub-Signature-256": f"sha256={signature}"},
            )
            return {"status": response.status_code, "json": response.json,
                    "engine_calls": len(ENGINE_CALLS), "sends": len(SENDS)}
        with client.session_transaction() as session:
            session["t_user_id"] = TENANT_ONE_OWNER_USER_ID
            session["t_email"] = "owner@example.test"
            session["t_exp"] = 2**40
            session["csrf_token"] = "probe-token"
        conversation_id = _conversation_id(wa_id)
        if phase == "pause":
            response = client.post(
                f"/t/{TENANT_ONE_ID}/inbox/{conversation_id}/mode",
                data={"mode": "paused", "csrf_token": "probe-token"},
            )
            return {"status": response.status_code}
        if phase == "inbox":
            response = client.get(f"/t/{TENANT_ONE_ID}/inbox/{conversation_id}")
            html = response.get_data(as_text=True)
            return {
                "status": response.status_code,
                "red_state": 'state-pill state-red' in html,
                "label": "Human handling - AI paused" in html,
                "resume_offered": 'name="mode" value="ai_active"' in html,
                "message_shown": "Do you have red velvet?" in html,
            }
    raise SystemExit(f"unknown phase {phase}")


if __name__ == "__main__":
    print("PROBE_RESULT " + json.dumps(main(*sys.argv[1:4])))
