"""Read-only local service harness for the menu, using the disposable database.

This calls the real menu service and real TenantAdmin reads. It does not send
WhatsApp messages; what a live webhook would do after building a reply is covered
separately by the signed webhook smoke test.
"""

from __future__ import annotations

import time

from platform_admin.config import Settings
from platform_admin.services import menu

TENANT = "11111111-1111-4111-8111-111111111111"
SENDER = "254700000001"


def show(label: str, reply: menu.MenuReply | None) -> None:
    print(f"\n=== {label} ===")
    if reply is None:
        print("None")
        return
    print(f"kind={reply.kind} header={reply.header!r}")
    print(reply.text)
    if reply.buttons:
        print("buttons=" + ", ".join(button["title"] for button in reply.buttons))
    if reply.sections:
        rows = reply.sections[0].get("rows", [])
        print("rows=" + ", ".join(row["title"] for row in rows))


def main() -> None:
    settings = Settings.from_env()
    document = menu.load_document(settings, TENANT)
    context = menu.MenuContext(
        settings=settings,
        tenant_id=TENANT,
        wa_id=SENDER,
        document=document,
        channel={
            "tenant_id": TENANT,
            "channel_id": "c0000000-0000-4000-8000-000000000001",
            "credentials_ref": f"tenants/{TENANT}/whatsapp",
        },
        phone_number_id="dev-phone-id-cindy-001",
        message_id="wamid.harness",
    )
    print(f"menu_enabled={menu.is_enabled(document)}")
    for command in (
        "menu",
        "Browse Products",
        "Place an Order",
        "My Order",
        "Delivery & Collection",
        "FAQs",
        "order MENU-HARNESS-1",
    ):
        show(command, menu.handle_text(context, command))
    show(
        "Browse Products -> standard-cake",
        menu.handle_action(
            context, menu.make_action("cat", menu.index_token(0, "standard-cake"))
        ),
    )
    show(
        "FAQ answer",
        menu.handle_action(
            context,
            menu.make_action("faq", menu.index_token(0, "Do you deliver?")),
        ),
    )
    show(
        "Expired action",
        menu.handle_action(
            context,
            menu.make_action(
                "main", now=time.time() - menu.MENU_TTL_SECONDS - 120
            ),
        ),
    )
    show(
        "Unknown action",
        menu.handle_action(
            context, menu.make_action("unknown", "x")
        ),
    )


if __name__ == "__main__":
    main()
