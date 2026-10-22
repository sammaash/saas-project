"""Local-only smoke requests for the Cindy Bakes menu webhook.

Run with the Flask app on 127.0.0.1:5055 and the local .env loaded into the
environment. No real WhatsApp request is made: WHATSAPP_SEND_ENABLED=0 makes every
delivery attempt fail closed before credentials or network access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from platform_admin.services import menu

BASE = "http://127.0.0.1:5055"
PHONE_NUMBER_ID = "dev-phone-id-cindy-001"
SENDER = "254700000001"
SECRET = os.environ["WHATSAPP_APP_SECRET"]


def post(payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        BASE + "/webhooks/whatsapp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def text(message_id: str, body: str) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"profile": {"name": "Local Menu Tester"}, "wa_id": SENDER}],
            "messages": [{
                "id": message_id,
                "from": SENDER,
                "timestamp": str(int(time.time())),
                "type": "text",
                "text": {"body": body},
            }],
        }}]}],
    }
    status, response = post(payload)
    print(f"{body!r} -> {status} {response}")


def tap(message_id: str, action_id: str, title: str) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"profile": {"name": "Local Menu Tester"}, "wa_id": SENDER}],
            "messages": [{
                "id": message_id,
                "from": SENDER,
                "timestamp": str(int(time.time())),
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": action_id, "title": title},
                },
            }],
        }}]}],
    }
    status, response = post(payload)
    print(f"tap {title!r} -> {status} {response}")


def main() -> None:
    nonce = uuid.uuid4().hex[:10]
    for label in (
        "menu",
        "Browse Products",
        "Place an Order",
        "My Order",
        "Delivery & Collection",
        "FAQs",
        "Talk to Staff",
        "order MENU-LOCAL-1",
        "not a menu command",
    ):
        text(f"wamid.local.{nonce}.{label.replace(' ', '_')}", label)

    category_id = menu.make_action("cat", menu.index_token(0, "standard-cake"))
    tap(f"wamid.local.{nonce}.category", category_id, "standard-cake")
    product_id = "3e8ae204-d4b3-4081-9d67-1520f9f68954"
    tap(f"wamid.local.{nonce}.product", menu.make_action("prod", product_id), "Black Forest")
    faq_id = menu.make_action("faq", menu.index_token(0, "Do you deliver?"))
    tap(f"wamid.local.{nonce}.faq", faq_id, "Do you deliver?")

    expired = menu.make_action(
        "main", now=time.time() - menu.MENU_TTL_SECONDS - 120
    )
    tap(f"wamid.local.{nonce}.expired", expired, "Expired")

    duplicate_id = f"wamid.local.{nonce}.duplicate"
    text(duplicate_id, "menu")
    text(duplicate_id, "menu")


if __name__ == "__main__":
    main()
