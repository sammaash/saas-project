"""Loading an existing client's saved AI configuration and updating it one field at a time.

The dashboard's rules tab is a configuration *manager*: an administrator opens an existing
client, sees the settings that are actually stored, and saves one field or one section. These
tests drive the real Flask app against a real PostgreSQL database (the same harness as
``test_platform_admin_app.py``) and assert on the stored documents afterwards, so a save that
quietly reset an unrelated setting fails here.

Every tenant used is created by the test and deleted again; the seeded Cindy Bakes tenant is
never written to.
"""

from __future__ import annotations

import copy
import json
import re
import unittest
import uuid

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity
from platform_admin.config import Settings
from platform_admin.services import audit as audit_service
from platform_admin.services import bot_config
from platform_admin.services import menu as menu_service
from saas.bot import TenantBotContextLoader
from saas_test_support import (
    PLATFORM_ADMIN_USER_ID,
    SaasDatabaseTestCase,
    _connect,
    app_backend_test_url,
    platform_admin_test_url,
)

SLUG_PREFIX = "config-test-"
PASSWORD = "correct horse"

#: The configuration a test tenant starts with: every section of the onboarding form is
#: populated, and each section holds a nested object so a wholesale replacement would be
#: visible. Written straight to ``tenant_settings``, so the assertions never depend on the
#: code under test to build their own starting point.
SAVED_CONFIGURATION: dict[str, dict] = {
    "bot_persona": {
        "tone": "Professional Short",
        "bot_name": "Lamps Assistant",
        "max_chars": 250,
        "max_lines": 2,
        "emoji": "no",
    },
    "business_hours": {
        "greeting": "Karibu Autolamps!",
        "timezone": "Africa/Nairobi",
        "human_hours": {
            "monday": {"is_open": True, "open": "08:00", "close": "18:00"},
            "saturday": {"is_open": True, "open": "09:00", "close": "13:00"},
            "sunday": {"is_open": False},
        },
        "human_offline_message": "Team yetu iko offline sai.",
        "bot_hours": "24/7",
    },
    "business_rules": {
        "description_paragraph": "Sisi huuza taa za gari Toyota na Nissan.",
        "pricing_rule": "Fixed",
        "discount_allowed": "Up to 5%",
        "out_of_stock": "Suggest alternative",
        "payment_methods": ["M-Pesa", "Cash"],
        "delivery": "Countrywide",
        "return_policy": "Return within 3 days if damaged",
        "sellable_products": ["Headlights", "Bulbs"],
        "order_flow_derived": ["confirm_part", "ask_quantity", "mpesa_payment", "ask_delivery"],
        "escalation": {"after_tries": 2, "transfer_to": "Owner", "whatsapp": "+254700111222"},
        "on_photo": "ask_car_model",
        "on_price_asked": "show_price_with_order_buttons",
        "pricing_visibility": "show_exact",
        "spam_rule": {"repeat_count": 3, "action": "send_to_human"},
    },
    "configuration": {
        "identity": {
            "whatsapp_display_name": "Autolamps Kenya",
            "business_type": "Auto Parts",
            "whatsapp_number": "+254700000000",
        },
        "config": {
            "provider": "meta_cloud_api",
            "window_24h_action": "send_template_to_reopen",
            "followup_template_name": "order_followup_1",
            "human_handover_number": "+254700123456",
            "auto_close_ticket_after": "48h",
        },
        "knowledge_base": {
            "website_link": "https://autolamps.co.ke",
            "scrape_options": {"include_subpages": True, "max_pages": 20},
            "faqs": [
                {"question": "Mnafungua saa ngapi?", "answer": "Mon-Sat 8am-6pm"},
                {"question": "Mko na delivery?", "answer": "Ndio, countrywide."},
            ],
            "documents": ["warranty.pdf", "returns.docx"],
            "extra_info": "Hatuna branch. Warranty 6 months.",
            "rag_priority": ["faqs", "products_sheet", "extra_info", "website", "documents"],
        },
    },
}

EMPTY_CONFIGURATION: dict[str, dict] = {
    "bot_persona": {},
    "business_hours": {},
    "business_rules": {},
    "configuration": {},
}


def _settings(**overrides) -> Settings:
    return Settings(
        secret_key="k" * 40,
        cookie_secure=False,
        app_database_url=app_backend_test_url(),
        platform_database_url=platform_admin_test_url(),
        **overrides,
    )


class FakeAuthenticator(Authenticator):
    def __init__(self, users: dict[str, tuple[str, str]]):
        self._users = users

    def login(self, email, password):
        entry = self._users.get(email)
        if not entry or entry[0] != password:
            raise AuthError("Invalid email or password.")
        return Identity(entry[1], email)


class ConfigurationManagerTestCase(SaasDatabaseTestCase):
    """A signed-in platform administrator and one tenant with a saved configuration."""

    def setUp(self):
        self._cleanup()
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "insert into public.platform_admins (user_id) values (%s) on conflict do nothing",
                (PLATFORM_ADMIN_USER_ID,),
            )
        conn.close()
        self.authenticator = FakeAuthenticator({
            "admin@example.com": (PASSWORD, PLATFORM_ADMIN_USER_ID),
        })
        self.app = create_app(_settings(), authenticator=self.authenticator)
        self.client = self.app.test_client()
        self.addCleanup(self._cleanup)
        self.login()
        self.tenant_id = self.create_client("Config Manager")

    # -- harness -------------------------------------------------------------
    @staticmethod
    def _cleanup():
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "select id from public.tenants where slug like %s", (SLUG_PREFIX + "%",)
            )
            ids = [row[0] for row in cursor.fetchall()]
            if ids:
                cursor.execute("delete from public.audit_log where tenant_id = any(%s)", (ids,))
                cursor.execute("delete from public.tenants where id = any(%s)", (ids,))
            cursor.execute(
                "delete from public.platform_admins where user_id = %s", (PLATFORM_ADMIN_USER_ID,)
            )
        conn.close()

    @staticmethod
    def token(client) -> str:
        with client.session_transaction() as session:
            return session["csrf_token"]

    def login(self, client=None, email="admin@example.com", password=PASSWORD):
        client = client or self.client
        client.get("/login")
        return client.post(
            "/login", data={"csrf_token": self.token(client), "email": email, "password": password}
        )

    def post(self, path, data=None, client=None):
        client = client or self.client
        payload = dict(data or {})
        payload["csrf_token"] = self.token(client)
        return client.post(path, data=payload)

    def page(self, path) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    @staticmethod
    def rendered_value(html: str, name: str) -> str | None:
        """The ``value`` attribute of an input, so a default cannot masquerade as saved."""
        match = re.search(rf'name="{name}"[^>]*\bvalue="([^"]*)"', html)
        return match.group(1) if match else None

    @staticmethod
    def rendered_text(html: str, name: str) -> str | None:
        """The body of a textarea, for the fields that carry their value that way."""
        match = re.search(rf'name="{name}"[^>]*>(.*?)</textarea>', html, re.S)
        return match.group(1) if match else None

    def create_client(self, name: str, documents: dict | None = None) -> str:
        """Create a tenant through the dashboard, then give it a saved configuration."""
        slug = SLUG_PREFIX + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        response = self.post("/tenants/new", {
            "name": name, "slug": slug, "timezone": "Africa/Nairobi", "currency": "KES",
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        tenant_id = re.search(r"/tenants/([0-9a-f-]{36})", response.headers["Location"]).group(1)
        self.save_configuration(
            tenant_id, SAVED_CONFIGURATION if documents is None else documents
        )
        return tenant_id

    @staticmethod
    def save_configuration(tenant_id: str, documents: dict) -> None:
        """Write the four documents directly, as an earlier operator session left them."""
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "update public.tenant_settings set timezone = %s, currency = %s, "
                "business_rules = %s::jsonb, bot_persona = %s::jsonb, "
                "business_hours = %s::jsonb, configuration = %s::jsonb where tenant_id = %s",
                (
                    "Africa/Nairobi", "KES",
                    json.dumps(documents["business_rules"]),
                    json.dumps(documents["bot_persona"]),
                    json.dumps(documents["business_hours"]),
                    json.dumps(documents["configuration"]),
                    tenant_id,
                ),
            )
        conn.close()

    @staticmethod
    def stored(tenant_id: str) -> dict:
        """The stored row, so a test can assert on what a save actually wrote."""
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "select timezone, currency, business_rules, bot_persona, business_hours, "
                "configuration, updated_at from public.tenant_settings where tenant_id = %s",
                (tenant_id,),
            )
            row = cursor.fetchone()
        conn.close()
        return {
            "timezone": row[0], "currency": row[1], "business_rules": row[2],
            "bot_persona": row[3], "business_hours": row[4], "configuration": row[5],
            "updated_at": row[6],
        }

    def section(self, section: str, **data):
        return self.post(f"/tenants/{self.tenant_id}/rules/{section}", data)

    def audit_actions(self, tenant_id: str) -> list[str]:
        with self.superuser() as cursor:
            cursor.execute(
                "select action from public.audit_log where tenant_id = %s order by id", (tenant_id,)
            )
            return [row[0] for row in cursor.fetchall()]

    def last_audit_metadata(self, tenant_id: str) -> dict:
        with self.superuser() as cursor:
            cursor.execute(
                "select metadata from public.audit_log where tenant_id = %s order by id desc limit 1",
                (tenant_id,),
            )
            return cursor.fetchone()[0]



class LoadSavedConfigurationTests(ConfigurationManagerTestCase):
    """Requirement 1: an existing client's configuration is loaded, not re-asked for."""

    def test_the_form_shows_the_saved_configuration(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        for expected in (
            "Autolamps Kenya", "Auto Parts", "Karibu Autolamps!", "Lamps Assistant",
            "Countrywide", "Return within 3 days if damaged", "https://autolamps.co.ke",
            "order_followup_1", "+254700111222", "+254700123456", "warranty.pdf",
            "Mnafungua saa ngapi?", "Hatuna branch. Warranty 6 months.",
        ):
            self.assertIn(expected, html, expected)

    def test_the_read_api_returns_the_saved_values(self):
        payload = self.client.get(f"/tenants/{self.tenant_id}/rules/config").get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tenant_id"], self.tenant_id)
        self.assertEqual(payload["version"], bot_config.version_of(self.stored(self.tenant_id)))
        self.assertEqual(payload["fields"]["greeting"], "Karibu Autolamps!")
        self.assertEqual(payload["fields"]["tone"], "Professional Short")
        self.assertEqual(payload["fields"]["payment_methods"], ["M-Pesa", "Cash"])
        self.assertEqual(payload["fields"]["escalation_whatsapp"], "+254700111222")
        self.assertEqual(payload["fields"]["faqs"][0]["question"], "Mnafungua saa ngapi?")
        self.assertEqual(payload["stored"]["business_rules"]["delivery"], "Countrywide")
        self.assertEqual(
            [item["name"] for item in payload["sections"]],
            ["identity", "persona", "hours", "rules", "logic", "api", "menu", "knowledge",
             "faq", "document"],
        )

    def test_an_unconfigured_client_shows_not_set_instead_of_invented_defaults(self):
        tenant_id = self.create_client("Sparse Client", documents=EMPTY_CONFIGURATION)
        payload = self.client.get(f"/tenants/{tenant_id}/rules/config").get_json()
        self.assertIsNone(payload["fields"]["tone"])
        self.assertIsNone(payload["fields"]["max_pages"])
        self.assertIsNone(payload["fields"]["escalation_tries"])
        self.assertIsNone(payload["fields"]["day_open_time_monday"])
        self.assertEqual(payload["fields"]["payment_methods"], [])
        self.assertEqual(payload["stored"]["business_rules"], {})

        html = self.page(f"/tenants/{tenant_id}/rules")
        self.assertIn("Not set", html)
        # A fabricated default would appear as a *value*; a real one only ever appears as a
        # placeholder, so an untouched field can never look saved.
        for name in (
            "max_pages", "escalation_tries", "spam_repeat", "website_link", "bot_name",
            "return_policy", "day_open_time_monday", "day_close_time_sunday",
        ):
            self.assertEqual(self.rendered_value(html, name), "", name)
        self.assertEqual(self.rendered_text(html, "greeting").strip(), "")
        self.assertEqual(self.rendered_text(html, "extra_info").strip(), "")

    def test_the_form_renders_the_saved_value_of_a_configured_field(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertEqual(self.rendered_value(html, "max_pages"), "20")
        self.assertEqual(self.rendered_value(html, "escalation_tries"), "2")
        self.assertEqual(self.rendered_value(html, "website_link"), "https://autolamps.co.ke")
        self.assertEqual(self.rendered_value(html, "day_open_time_monday"), "08:00")
        self.assertEqual(self.rendered_text(html, "greeting").strip(), "Karibu Autolamps!")

    def test_a_setting_that_is_unset_is_not_replaced_by_a_default_on_save(self):
        tenant_id = self.create_client("Unset Client", documents=EMPTY_CONFIGURATION)
        response = self.post(
            f"/tenants/{tenant_id}/rules/knowledge", {"extra_info": "Warranty 6 months."}
        )
        self.assertEqual(response.status_code, 302)
        knowledge = self.stored(tenant_id)["configuration"]["knowledge_base"]
        self.assertEqual(knowledge, {"extra_info": "Warranty 6 months."})

    def test_the_stored_documents_are_shown_in_the_raw_editors(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("ask_car_model", html)
        self.assertIn("sellable_products", html)



class PartialUpdateTests(ConfigurationManagerTestCase):
    """Requirements 2 and 3: a save writes only what was submitted."""

    def test_changing_one_field_changes_only_that_field(self):
        before = self.stored(self.tenant_id)
        response = self.section("persona", tone="Friendly Sheng")
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        after = self.stored(self.tenant_id)

        self.assertEqual(after["bot_persona"]["tone"], "Friendly Sheng")
        self.assertEqual(after["bot_persona"]["bot_name"], "Lamps Assistant")
        for column in ("business_rules", "business_hours", "configuration"):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual((after["timezone"], after["currency"]), ("Africa/Nairobi", "KES"))

    def test_changing_the_greeting_leaves_every_other_section_alone(self):
        before = self.stored(self.tenant_id)
        response = self.section("hours", greeting="Habari! Karibu Autolamps.")
        self.assertEqual(response.status_code, 302)
        after = self.stored(self.tenant_id)

        self.assertEqual(after["business_hours"]["greeting"], "Habari! Karibu Autolamps.")
        self.assertEqual(
            after["business_hours"]["human_hours"], before["business_hours"]["human_hours"]
        )
        for column in ("business_rules", "bot_persona", "configuration"):
            self.assertEqual(after[column], before[column], column)

    def test_a_nested_field_save_preserves_its_siblings(self):
        self.assertEqual(
            self.section("rules", escalation_whatsapp="+254711999888").status_code, 302
        )
        escalation = self.stored(self.tenant_id)["business_rules"]["escalation"]
        self.assertEqual(
            escalation,
            {"after_tries": 2, "transfer_to": "Owner", "whatsapp": "+254711999888"},
        )

    def test_saving_one_day_preserves_the_other_days(self):
        before = self.stored(self.tenant_id)["business_hours"]["human_hours"]
        response = self.section(
            "hours",
            day_open_saturday="1",
            day_open_time_saturday="10:00",
            day_close_time_saturday="14:00",
        )
        self.assertEqual(response.status_code, 302)
        after = self.stored(self.tenant_id)["business_hours"]["human_hours"]

        self.assertEqual(after["saturday"], {"is_open": True, "open": "10:00", "close": "14:00"})
        for day in ("monday", "sunday"):
            self.assertEqual(after[day], before[day], day)

    def test_closing_a_day_drops_its_hours_instead_of_keeping_stale_times(self):
        response = self.section(
            "hours",
            day_open_monday="0",
            day_open_time_monday="08:00",
            day_close_time_monday="18:00",
        )
        self.assertEqual(response.status_code, 302)
        monday = self.stored(self.tenant_id)["business_hours"]["human_hours"]["monday"]
        self.assertEqual(monday, {"is_open": False})

    def test_the_derived_order_flow_follows_the_payment_and_delivery_rules(self):
        self.assertEqual(self.section("rules", delivery="Pickup only").status_code, 302)
        self.assertEqual(
            self.stored(self.tenant_id)["business_rules"]["order_flow_derived"],
            ["confirm_part", "ask_quantity", "mpesa_payment"],
        )

    def test_a_section_save_that_touches_no_derived_field_leaves_the_flow_alone(self):
        before = self.stored(self.tenant_id)["business_rules"]["order_flow_derived"]
        self.assertEqual(self.section("rules", return_policy="14 days").status_code, 302)
        self.assertEqual(
            self.stored(self.tenant_id)["business_rules"]["order_flow_derived"], before
        )

    def test_the_order_flow_is_never_asked_for(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("Derived:", html)
        self.assertNotIn('name="order_flow', html)



class EmptyAndOmittedValueTests(ConfigurationManagerTestCase):
    """Requirement 5: an omitted field is untouched; an empty one is an explicit clear."""

    def test_an_omitted_field_is_left_exactly_as_it_was(self):
        before = self.stored(self.tenant_id)["business_rules"]
        self.assertEqual(self.section("rules", out_of_stock="Take order").status_code, 302)
        after = self.stored(self.tenant_id)["business_rules"]

        self.assertEqual(after["out_of_stock"], "Take order")
        for key, value in before.items():
            if key != "out_of_stock":
                self.assertEqual(after[key], value, key)

    def test_an_explicitly_empty_field_is_cleared(self):
        self.assertEqual(self.section("rules", return_policy="").status_code, 302)
        self.assertNotIn("return_policy", self.stored(self.tenant_id)["business_rules"])

    def test_an_explicitly_empty_choice_is_cleared(self):
        self.assertEqual(self.section("logic", pricing_visibility="").status_code, 302)
        self.assertNotIn("pricing_visibility", self.stored(self.tenant_id)["business_rules"])

    def test_clearing_an_unset_field_is_a_no_op_rather_than_an_error(self):
        tenant_id = self.create_client("Clear Client", documents=EMPTY_CONFIGURATION)
        response = self.post(f"/tenants/{tenant_id}/rules/logic", {"pricing_visibility": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(tenant_id)["business_rules"], {})

    def test_an_emptied_checkbox_group_is_emptied_not_ignored(self):
        response = self.section("rules", present_payment_methods="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(self.tenant_id)["business_rules"]["payment_methods"], [])

    def test_a_multi_value_field_can_be_replaced_wholesale(self):
        response = self.section("rules", present_payment_methods="1", payment_methods=["Cash"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.stored(self.tenant_id)["business_rules"]["payment_methods"], ["Cash"]
        )

    def test_a_section_save_with_no_fields_is_refused(self):
        before = self.stored(self.tenant_id)
        response = self.section("persona")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no changes", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)


class ValidationTests(ConfigurationManagerTestCase):
    """Requirement 6: an invalid change is rejected and the stored value is untouched."""

    def test_an_unknown_choice_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section("persona", tone="Chatty")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Tone must be one of", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_malformed_time_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section(
            "hours",
            day_open_monday="1",
            day_open_time_monday="25:00",
            day_close_time_monday="18:00",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be a time such as 08:00", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_malformed_phone_number_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section("rules", escalation_whatsapp="not-a-number")
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be a phone number", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_website_link_without_a_scheme_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section("knowledge", website_link="autolamps.co.ke")
        self.assertEqual(response.status_code, 400)
        self.assertIn("must start with http://", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_an_out_of_range_number_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section("knowledge", max_pages="500")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Max pages must be at most 100", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_failed_save_keeps_the_submitted_values_on_screen(self):
        response = self.section("persona", tone="Chatty", bot_name="Mzalendo")
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn("Mzalendo", html)
        self.assertIn("Tone must be one of", html)

    def test_a_failed_audit_write_rolls_the_whole_change_back(self):
        """The audit event and the change share one transaction, so neither lands alone."""
        before = self.stored(self.tenant_id)

        def explode(*_args, **_kwargs):
            raise RuntimeError("audit unavailable")

        original = audit_service.record
        audit_service.record = explode
        try:
            response = self.section("persona", tone="Friendly Sheng")
        finally:
            audit_service.record = original

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.stored(self.tenant_id), before)



class KnowledgeBaseTests(ConfigurationManagerTestCase):
    """Requirement 4: knowledge-base and catalogue operations are additive, never wholesale."""

    def test_editing_one_faq_leaves_the_other_faqs_alone(self):
        before = self.stored(self.tenant_id)["configuration"]["knowledge_base"]["faqs"]
        response = self.section(
            "faq",
            faq_index="0",
            faq_question="Mnafungua saa ngapi?",
            faq_answer="Mon-Sat 8am-6pm, Sunday closed.",
        )
        self.assertEqual(response.status_code, 302)
        after = self.stored(self.tenant_id)["configuration"]["knowledge_base"]["faqs"]

        self.assertEqual(len(after), len(before))
        self.assertEqual(after[1], before[1])
        self.assertEqual(
            after[0],
            {"question": "Mnafungua saa ngapi?", "answer": "Mon-Sat 8am-6pm, Sunday closed."},
        )

    def test_removing_one_faq_leaves_the_other_faqs_alone(self):
        response = self.section("faq", faq_index="1", faq_remove="1")
        self.assertEqual(response.status_code, 302)
        faqs = self.stored(self.tenant_id)["configuration"]["knowledge_base"]["faqs"]
        self.assertEqual([faq["question"] for faq in faqs], ["Mnafungua saa ngapi?"])

    def test_editing_a_faq_that_no_longer_exists_is_rejected(self):
        before = self.stored(self.tenant_id)
        response = self.section("faq", faq_index="9", faq_question="Q?", faq_answer="A")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer exists", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_removing_a_document_is_an_explicit_single_item_action(self):
        response = self.section("document", document_name="warranty.pdf")
        self.assertEqual(response.status_code, 302)
        knowledge = self.stored(self.tenant_id)["configuration"]["knowledge_base"]
        self.assertEqual(knowledge["documents"], ["returns.docx"])
        self.assertEqual(
            knowledge["faqs"], SAVED_CONFIGURATION["configuration"]["knowledge_base"]["faqs"]
        )

    def test_changing_the_website_keeps_the_documents_and_the_faqs(self):
        before = self.stored(self.tenant_id)["configuration"]["knowledge_base"]
        response = self.section("knowledge", website_link="https://new.example.com")
        self.assertEqual(response.status_code, 302)
        after = self.stored(self.tenant_id)["configuration"]["knowledge_base"]

        self.assertEqual(after["website_link"], "https://new.example.com")
        for key in ("documents", "faqs", "extra_info", "scrape_options", "rag_priority"):
            self.assertEqual(after[key], before[key], key)

    def test_extra_information_changes_without_touching_policies_or_documents(self):
        before = self.stored(self.tenant_id)["configuration"]["knowledge_base"]
        response = self.section("knowledge", extra_info="Warranty 12 months.")
        self.assertEqual(response.status_code, 302)
        after = self.stored(self.tenant_id)["configuration"]["knowledge_base"]

        self.assertEqual(after["extra_info"], "Warranty 12 months.")
        for key in ("website_link", "documents", "faqs", "scrape_options"):
            self.assertEqual(after[key], before[key], key)

    def test_a_configuration_save_never_touches_the_catalogue(self):
        base = f"/tenants/{self.tenant_id}"
        response = self.post(f"{base}/catalogue/products", {
            "name": "Headlight", "category": "Headlights", "sort_order": "1"})
        self.assertEqual(response.status_code, 302)
        with self.superuser() as cursor:
            cursor.execute(
                "select count(*) from public.products where tenant_id = %s", (self.tenant_id,)
            )
            self.assertEqual(cursor.fetchone()[0], 1)

        self.assertEqual(
            self.section("knowledge", extra_info="Warranty 12 months.").status_code, 302
        )
        self.assertEqual(self.section("rules", out_of_stock="Take order").status_code, 302)

        with self.superuser() as cursor:
            cursor.execute(
                "select name, category from public.products where tenant_id = %s", (self.tenant_id,)
            )
            self.assertEqual(cursor.fetchall(), [("Headlight", "Headlights")])

    def test_saved_sellable_categories_survive_a_catalogue_that_no_longer_lists_them(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("Headlights", html)
        self.assertIn("saved, not in the current catalogue", html)

        self.assertEqual(self.section("rules", out_of_stock="Take order").status_code, 302)
        self.assertEqual(
            self.stored(self.tenant_id)["business_rules"]["sellable_products"],
            ["Headlights", "Bulbs"],
        )



class SecretHandlingTests(ConfigurationManagerTestCase):
    """Requirement 5: credentials never reach the browser, and masking never destroys them."""

    TOKEN = "fixture-credential-value-not-a-real-token"

    def _client_with_a_stored_credential(self) -> str:
        documents = copy.deepcopy(SAVED_CONFIGURATION)
        documents["configuration"]["config"]["whatsapp_access_token"] = self.TOKEN
        return self.create_client("Secret Client", documents=documents)

    def test_a_stored_credential_is_never_rendered_on_the_page(self):
        tenant_id = self._client_with_a_stored_credential()
        html = self.page(f"/tenants/{tenant_id}/rules")
        self.assertNotIn(self.TOKEN, html)
        self.assertIn("[redacted]", html)

    def test_a_stored_credential_is_never_returned_by_the_read_api(self):
        tenant_id = self._client_with_a_stored_credential()
        body = self.client.get(f"/tenants/{tenant_id}/rules/config").get_data(as_text=True)
        self.assertNotIn(self.TOKEN, body)
        self.assertEqual(
            json.loads(body)["stored"]["configuration"]["config"]["whatsapp_access_token"],
            "[redacted]",
        )

    def test_saving_the_masked_raw_editor_keeps_the_stored_credential(self):
        tenant_id = self._client_with_a_stored_credential()
        stored = self.client.get(f"/tenants/{tenant_id}/rules/config").get_json()["stored"]
        response = self.post(f"/tenants/{tenant_id}/rules", {
            "business_rules": json.dumps(stored["business_rules"]),
            "bot_persona": json.dumps(stored["bot_persona"]),
            "business_hours": json.dumps(stored["business_hours"]),
            "configuration": json.dumps(stored["configuration"]),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.stored(tenant_id)["configuration"]["config"]["whatsapp_access_token"], self.TOKEN
        )

    def test_a_section_save_keeps_a_credential_the_section_does_not_manage(self):
        tenant_id = self._client_with_a_stored_credential()
        response = self.post(f"/tenants/{tenant_id}/rules/api", {"auto_close_ticket_after": "72h"})
        self.assertEqual(response.status_code, 302)
        config = self.stored(tenant_id)["configuration"]["config"]
        self.assertEqual(config["auto_close_ticket_after"], "72h")
        self.assertEqual(config["whatsapp_access_token"], self.TOKEN)



class MultiTenantSecurityTests(ConfigurationManagerTestCase):
    """Requirement 5: every read and write is scoped to the authorised tenant."""

    def test_a_signed_out_visitor_cannot_read_or_write(self):
        client = self.app.test_client()
        self.assertEqual(client.get(f"/tenants/{self.tenant_id}/rules").status_code, 302)
        self.assertEqual(client.get(f"/tenants/{self.tenant_id}/rules/config").status_code, 302)

        client.get("/login")  # issues a CSRF token without signing anybody in
        before = self.stored(self.tenant_id)
        response = client.post(f"/tenants/{self.tenant_id}/rules/persona", data={
            "csrf_token": self.token(client), "tone": "Formal"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_write_without_a_csrf_token_is_refused_before_anything_else(self):
        client = self.app.test_client()
        before = self.stored(self.tenant_id)
        response = client.post(f"/tenants/{self.tenant_id}/rules/persona", data={"tone": "Formal"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_save_targets_the_selected_client_and_no_other(self):
        other = self.create_client("Other Client")
        before_other = self.stored(other)
        self.assertEqual(self.section("persona", tone="Formal").status_code, 302)
        self.assertEqual(self.stored(other), before_other)
        self.assertEqual(self.stored(self.tenant_id)["bot_persona"]["tone"], "Formal")

    def test_an_unknown_tenant_is_not_found(self):
        unknown = uuid.uuid4()
        self.assertEqual(self.client.get(f"/tenants/{unknown}/rules").status_code, 404)
        self.assertEqual(self.client.get(f"/tenants/{unknown}/rules/config").status_code, 404)
        self.assertEqual(
            self.post(f"/tenants/{unknown}/rules/persona", {"tone": "Formal"}).status_code, 404
        )

    def test_an_unknown_section_is_not_found(self):
        response = self.section("not-a-section", tone="Formal")
        self.assertEqual(response.status_code, 404)

    def test_the_tenant_id_from_the_browser_is_validated_not_trusted(self):
        before = self.stored(self.tenant_id)
        # A malformed tenant id must be refused by validation, never passed to the database.
        response = self.post("/tenants/not-a-uuid/rules/persona", {"tone": "Formal"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(self.tenant_id), before)


class ConcurrencyTests(ConfigurationManagerTestCase):
    """Requirement 6: two administrators editing one configuration cannot silently clobber."""

    def test_a_stale_form_version_is_rejected_and_nothing_is_written(self):
        stale = bot_config.version_of(self.stored(self.tenant_id))
        self.assertEqual(self.section("persona", tone="Formal").status_code, 302)
        before = self.stored(self.tenant_id)

        response = self.section("persona", tone="Friendly Sheng", config_version=stale)
        self.assertEqual(response.status_code, 409)
        self.assertIn("changed in another session", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_the_version_the_page_rendered_is_accepted(self):
        current = bot_config.version_of(self.stored(self.tenant_id))
        response = self.section("persona", tone="Formal", config_version=current)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(self.tenant_id)["bot_persona"]["tone"], "Formal")

    def test_the_page_renders_the_version_of_the_stored_configuration(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn(f'name="config_version" value="{bot_config.version_of(self.stored(self.tenant_id))}"', html)


class AuditTests(ConfigurationManagerTestCase):
    def test_a_section_save_records_the_section_and_the_fields_it_touched(self):
        self.assertEqual(self.section("persona", tone="Formal").status_code, 302)
        self.assertIn("tenant.config_updated", self.audit_actions(self.tenant_id))
        metadata = self.last_audit_metadata(self.tenant_id)
        self.assertEqual(metadata["section"], "persona")
        self.assertEqual(
            sorted(metadata["fields"]), ["bot_persona.emoji", "bot_persona.max_chars",
                                        "bot_persona.max_lines", "bot_persona.tone"],
        )




class JsonPatchContractTests(ConfigurationManagerTestCase):
    """Requirement 3: an explicit partial-update contract, not only a whole-form post."""

    def patch(self, path, payload, *, with_token=True):
        headers = {"X-CSRF-Token": self.token(self.client)} if with_token else {}
        return self.client.patch(path, json=payload, headers=headers)

    def test_a_json_patch_changes_only_the_named_field(self):
        before = self.stored(self.tenant_id)
        response = self.patch(f"/tenants/{self.tenant_id}/rules/persona", {"tone": "Formal"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fields"]["tone"], "Formal")

        after = self.stored(self.tenant_id)
        self.assertEqual(after["bot_persona"]["tone"], "Formal")
        self.assertEqual(after["business_rules"], before["business_rules"])
        self.assertEqual(after["configuration"], before["configuration"])

    def test_a_json_patch_uses_the_same_field_names_as_the_form(self):
        """One contract, not two: the API names fields exactly as the section forms do."""
        response = self.patch(
            f"/tenants/{self.tenant_id}/rules/rules",
            {"escalation_whatsapp": "+254733000111"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        escalation = self.stored(self.tenant_id)["business_rules"]["escalation"]
        self.assertEqual(escalation["whatsapp"], "+254733000111")
        self.assertEqual(escalation["after_tries"], 2)
        self.assertEqual(escalation["transfer_to"], "Owner")

    def test_a_json_patch_that_names_no_field_is_refused(self):
        before = self.stored(self.tenant_id)
        response = self.patch(f"/tenants/{self.tenant_id}/rules/rules", {"escalation": {}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_json_patch_without_a_csrf_token_is_refused(self):
        response = self.patch(
            f"/tenants/{self.tenant_id}/rules/persona", {"tone": "Formal"}, with_token=False
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored(self.tenant_id)["bot_persona"]["tone"], "Professional Short")

    def test_a_json_patch_reports_a_validation_failure_without_writing(self):
        before = self.stored(self.tenant_id)
        response = self.patch(f"/tenants/{self.tenant_id}/rules/persona", {"tone": "Chatty"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_a_json_patch_of_an_unknown_section_is_not_found(self):
        response = self.patch(f"/tenants/{self.tenant_id}/rules/nope", {"tone": "Formal"})
        self.assertEqual(response.status_code, 404)


class RuntimeEffectTests(ConfigurationManagerTestCase):
    """Requirement 9: a saved change is what the bot runtime reads on its next reply."""

    def test_the_bot_context_loader_sees_a_saved_change_immediately(self):
        loader = TenantBotContextLoader(app_backend_test_url())
        self.assertEqual(
            loader.load(self.tenant_id).settings["business_hours"]["greeting"], "Karibu Autolamps!"
        )
        self.assertEqual(self.section("hours", greeting="Habari! Karibu.").status_code, 302)

        context = loader.load(self.tenant_id)
        self.assertEqual(context.settings["business_hours"]["greeting"], "Habari! Karibu.")
        self.assertEqual(context.settings["business_rules"]["delivery"], "Countrywide")
        self.assertEqual(context.settings["bot_persona"]["tone"], "Professional Short")
        self.assertEqual(
            context.settings["configuration"]["knowledge_base"]["documents"],
            ["warranty.pdf", "returns.docx"],
        )

    def test_a_bot_context_never_carries_another_tenants_settings(self):
        other = self.create_client("Runtime Other Client")
        loader = TenantBotContextLoader(app_backend_test_url())
        self.assertEqual(
            loader.load(other).settings["business_hours"]["greeting"], "Karibu Autolamps!"
        )
        self.assertEqual(
            self.section("hours", greeting="Only for the first client.").status_code, 302
        )
        self.assertEqual(
            loader.load(other).settings["business_hours"]["greeting"], "Karibu Autolamps!"
        )



class OnboardingRegressionTests(ConfigurationManagerTestCase):
    """Requirement 11: the original new-client workflow still works end to end."""

    def test_the_original_form_still_saves_every_section_at_once(self):
        tenant_id = self.create_client("Onboarding Client", documents=EMPTY_CONFIGURATION)
        response = self.post(f"/tenants/{tenant_id}/rules", {
            "form_kind": "whatsapp_onboarding",
            "wa_display_name": "Onboarding Client", "business_type": "Bakery",
            "whatsapp_number": "+254700000001",
            "tone": "Formal", "bot_name": "Baker",
            "greeting": "Karibu!", "human_offline_message": "Tuko offline.",
            "day_open_monday": "1", "day_open_time_monday": "07:00",
            "day_close_time_monday": "19:00",
            "business_description": "Tunauza keki.", "pricing_rule": "Fixed",
            "out_of_stock": "Take order", "payment_methods": ["M-Pesa"],
            "delivery": "Countrywide", "return_policy": "3 days",
            "escalation_tries": "2", "escalation_transfer": "Owner",
            "on_photo": "ask_car_model", "on_price_asked": "show_price_with_order_buttons",
            "pricing_visibility": "show_exact", "spam_repeat": "3",
            "provider": "meta_cloud_api", "window_24h_action": "send_template_to_reopen",
            "followup_template_name": "order_followup_1", "auto_close_ticket_after": "48h",
            "website_link": "https://example.com", "max_pages": "10",
            "faqs_bulk": "Q: Mnafungua saa ngapi?\nA: Mon-Sat 8am-6pm",
            "extra_info": "Warranty 6 months.",
        })
        self.assertEqual(response.status_code, 302)
        stored = self.stored(tenant_id)

        self.assertEqual(stored["bot_persona"]["tone"], "Formal")
        self.assertEqual(stored["bot_persona"]["max_chars"], 350)
        self.assertEqual(stored["business_hours"]["greeting"], "Karibu!")
        self.assertEqual(
            stored["business_hours"]["human_hours"]["monday"],
            {"is_open": True, "open": "07:00", "close": "19:00"},
        )
        self.assertEqual(stored["business_rules"]["payment_methods"], ["M-Pesa"])
        self.assertEqual(stored["business_rules"]["out_of_stock"], "Take order")
        self.assertEqual(stored["configuration"]["identity"]["business_type"], "Bakery")
        self.assertEqual(
            stored["configuration"]["knowledge_base"]["faqs"],
            [{"question": "Mnafungua saa ngapi?", "answer": "Mon-Sat 8am-6pm"}],
        )
        self.assertIn("tenant.rules_updated", self.audit_actions(tenant_id))

    def test_the_original_form_still_requires_the_onboarding_fields(self):
        tenant_id = self.create_client("Incomplete Client", documents=EMPTY_CONFIGURATION)
        response = self.post(f"/tenants/{tenant_id}/rules", {
            "form_kind": "whatsapp_onboarding",
            "wa_display_name": "", "business_type": "", "tone": "", "greeting": "",
            "business_description": "",
        })
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("is required", page)
        self.assertEqual(self.stored(tenant_id)["bot_persona"], {})

    def test_a_new_client_can_be_configured_section_by_section(self):
        tenant_id = self.create_client("Section Client", documents=EMPTY_CONFIGURATION)
        for section, data in (
            ("identity", {"wa_display_name": "Section Client", "business_type": "Bakery"}),
            ("persona", {"tone": "Friendly Sheng", "bot_name": "Baker"}),
            ("hours", {"greeting": "Karibu!"}),
            ("rules", {"out_of_stock": "Take order"}),
        ):
            response = self.post(f"/tenants/{tenant_id}/rules/{section}", data)
            self.assertEqual(response.status_code, 302, section)
        stored = self.stored(tenant_id)
        self.assertEqual(stored["configuration"]["identity"]["business_type"], "Bakery")
        self.assertEqual(stored["bot_persona"]["tone"], "Friendly Sheng")
        self.assertEqual(stored["business_hours"]["greeting"], "Karibu!")
        self.assertEqual(stored["business_rules"], {"out_of_stock": "Take order"})


class AcceptanceCriterionTests(ConfigurationManagerTestCase):
    """The stated acceptance criterion, end to end."""

    def test_change_only_the_greeting_then_reopen_and_verify_everything_else_is_intact(self):
        before = self.stored(self.tenant_id)

        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("Karibu Autolamps!", html)

        response = self.section("hours", greeting="Karibu Autolamps! Tuma picha.")
        self.assertEqual(response.status_code, 302)

        reopened = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("Karibu Autolamps! Tuma picha.", reopened)

        after = self.stored(self.tenant_id)
        self.assertEqual(after["business_hours"]["greeting"], "Karibu Autolamps! Tuma picha.")
        for column in ("business_rules", "bot_persona", "configuration"):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(
            after["business_hours"]["human_hours"], before["business_hours"]["human_hours"]
        )
        self.assertEqual(after["timezone"], before["timezone"])
        self.assertEqual(after["currency"], before["currency"])


class MenuControlTests(ConfigurationManagerTestCase):
    """The dashboard menu toggle uses the existing section-save contract."""

    @staticmethod
    def _menu_control(html: str) -> str:
        match = re.search(r'<input[^>]*name="menu_enabled"[^>]*>', html)
        if match is None:
            raise AssertionError("menu_enabled control not found")
        return match.group(0)

    def test_menu_is_disabled_by_default_and_control_is_visible(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("H. WhatsApp customer menu", html)
        self.assertIn("Enable WhatsApp Menu", html)
        self.assertNotIn("checked", self._menu_control(html))
        payload = self.client.get(f"/tenants/{self.tenant_id}/rules/config").get_json()
        self.assertFalse(payload["fields"]["menu_enabled"])
        self.assertFalse(menu_service.is_enabled(self.stored(self.tenant_id)))

    def test_enabling_and_disabling_persist_without_overwriting_other_settings(self):
        documents = copy.deepcopy(SAVED_CONFIGURATION)
        documents["configuration"]["menu"] = {"enabled": False, "custom": "keep-me"}
        documents["configuration"]["config"]["whatsapp_access_token"] = "local-secret"
        tenant_id = self.create_client("Menu Control", documents=documents)
        before = self.stored(tenant_id)

        response = self.post(
            f"/tenants/{tenant_id}/rules/menu",
            {"present_menu_enabled": "1", "menu_enabled": "1"},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        after = self.stored(tenant_id)
        self.assertTrue(after["configuration"]["menu"]["enabled"])
        self.assertEqual(after["configuration"]["menu"]["custom"], "keep-me")
        self.assertEqual(
            after["configuration"]["config"]["whatsapp_access_token"], "local-secret"
        )
        self.assertTrue(menu_service.is_enabled(after))
        self.assertEqual(
            self.client.get(f"/tenants/{tenant_id}/rules/config").get_json()["fields"][
                "menu_enabled"
            ],
            True,
        )
        self.assertIn("checked", self._menu_control(self.page(f"/tenants/{tenant_id}/rules")))

        before_config = copy.deepcopy(before["configuration"])
        after_config = copy.deepcopy(after["configuration"])
        before_config["menu"]["enabled"] = True
        self.assertEqual(after_config, before_config)
        for column in ("business_rules", "bot_persona", "business_hours"):
            self.assertEqual(after[column], before[column], column)

        response = self.post(
            f"/tenants/{tenant_id}/rules/menu", {"present_menu_enabled": "1"}
        )
        self.assertEqual(response.status_code, 302)
        disabled = self.stored(tenant_id)
        self.assertFalse(disabled["configuration"]["menu"]["enabled"])
        self.assertEqual(disabled["configuration"]["menu"]["custom"], "keep-me")
        self.assertFalse(menu_service.is_enabled(disabled))
        self.assertNotIn("checked", self._menu_control(self.page(f"/tenants/{tenant_id}/rules")))

    def test_invalid_boolean_is_rejected_without_writing(self):
        before = self.stored(self.tenant_id)
        response = self.post(
            f"/tenants/{self.tenant_id}/rules/menu",
            {"present_menu_enabled": "1", "menu_enabled": "maybe"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be true or false", response.get_data(as_text=True))
        self.assertEqual(self.stored(self.tenant_id), before)

    def test_menu_change_is_scoped_to_the_selected_tenant(self):
        other = self.create_client("Menu Other Tenant", documents=EMPTY_CONFIGURATION)
        before_other = self.stored(other)
        response = self.section("menu", present_menu_enabled="1", menu_enabled="1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(menu_service.is_enabled(self.stored(self.tenant_id)))
        self.assertEqual(self.stored(other), before_other)
        self.assertFalse(menu_service.is_enabled(self.stored(other)))

    def test_signed_out_user_cannot_change_the_menu(self):
        client = self.app.test_client()
        client.get("/login")  # issue a CSRF token without authenticating
        before = self.stored(self.tenant_id)
        response = client.post(
            f"/tenants/{self.tenant_id}/rules/menu",
            data={
                "csrf_token": self.token(client),
                "present_menu_enabled": "1",
                "menu_enabled": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(self.tenant_id), before)
        self.assertFalse(menu_service.is_enabled(self.stored(self.tenant_id)))


class MenuBuilderTests(ConfigurationManagerTestCase):
    """The Rules / AI menu builder writes validated tenant-local menu JSON."""

    @staticmethod
    def _menu_form(options):
        data = {
            "present_menu_enabled": "1",
            "menu_enabled": "1",
            "present_menu_options": "1",
        }
        for index, option in enumerate(options):
            data[f"option_id_{index}"] = option.get("id", "")
            data[f"option_label_{index}"] = option.get("label", "")
            data[f"option_action_{index}"] = option.get("action", "")
            if option.get("enabled", True):
                data[f"option_enabled_{index}"] = "1"
            data[f"option_position_{index}"] = str(option.get("position", index + 1))
        return data

    @staticmethod
    def _default_options():
        return [
            {
                "id": option.id,
                "label": option.label,
                "action": option.action,
                "enabled": option.enabled,
                "position": option.position,
            }
            for option in menu_service.default_menu_options()
        ]

    def test_builder_renders_default_options_and_preview(self):
        html = self.page(f"/tenants/{self.tenant_id}/rules")
        self.assertIn("Menu Builder", html)
        self.assertIn("Preview before saving", html)
        self.assertIn("Browse Products", html)
        payload = self.client.get(f"/tenants/{self.tenant_id}/rules/config").get_json()
        self.assertEqual(len(payload["fields"]["menu_options"]), 6)
        self.assertTrue(payload["fields"]["menu_options"][0]["enabled"])
        self.assertFalse(menu_service.is_enabled(self.stored(self.tenant_id)))

    def test_builder_saves_custom_welcome_reordered_renamed_and_disabled_options(self):
        data = {
            "present_menu_enabled": "1",
            "menu_enabled": "1",
            "present_menu_options": "1",
            "menu_welcome_message": "Karibu!",
            "menu_language": "sw",
            "option_id_0": "browse_products",
            "option_label_0": "Shop",
            "option_action_0": "browse_products",
            "option_enabled_0": "1",
            "option_position_0": "2",
            "option_id_1": "my_order",
            "option_label_1": "Track order",
            "option_action_1": "my_order",
            "option_enabled_1": "1",
            "option_position_1": "1",
            "option_id_2": "faqs",
            "option_label_2": "Questions",
            "option_action_2": "faqs",
            "option_position_2": "3",
        }
        response = self.section("menu", **data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        stored = self.stored(self.tenant_id)
        config = menu_service.menu_config(stored)
        self.assertTrue(config.enabled)
        self.assertEqual(config.welcome_message, "Karibu!")
        self.assertEqual(config.language, "sw")
        self.assertEqual([option.label for option in config.options],
                         ["Track order", "Shop", "Questions"])
        self.assertFalse(config.options[2].enabled)
        main = menu_service.main_menu(stored, now=1000)
        self.assertEqual([row["title"] for row in main.sections[0]["rows"]],
                         ["Track order", "Shop"])
        self.assertTrue(all(row["id"].startswith("m1.option.")
                            for row in main.sections[0]["rows"]))
        self.assertIn("Karibu!", main.text)

    def test_builder_rejects_duplicate_ids_labels_actions_and_positions(self):
        before = self.stored(self.tenant_id)
        base = {
            "present_menu_enabled": "1",
            "menu_enabled": "1",
            "present_menu_options": "1",
            "option_label_0": "Shop",
            "option_action_0": "browse_products",
            "option_enabled_0": "1",
            "option_position_0": "1",
        }
        cases = (
            ({"option_id_0": "shop", "option_id_1": "shop",
              "option_label_1": "Other", "option_action_1": "faqs",
              "option_position_1": "2"}, "Duplicate menu option id"),
            ({"option_id_0": "shop", "option_label_0": "",
              "option_action_0": "browse_products",
              "option_position_0": "1"}, "label is required"),
            ({"option_id_0": "shop", "option_action_0": "run_code",
              "option_label_0": "Shop", "option_position_0": "1"}, "unsupported action"),
            ({"option_id_0": "shop", "option_action_0": "browse_products",
              "option_label_0": "Shop", "option_position_0": "0"}, "position must be"),
            ({"option_id_0": "shop", "option_action_0": "browse_products",
              "option_label_0": "Shop", "option_position_0": "11"}, "position must be"),
        )
        for values, message in cases:
            data = {**base, **values}
            with self.subTest(message=message):
                response = self.section("menu", **data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_data(as_text=True))
                self.assertEqual(self.stored(self.tenant_id), before)

    def test_default_option_six_has_a_stable_valid_id_and_staff_action(self):
        options = menu_service.default_menu_options()
        self.assertEqual(len(options), 6)
        for option in options:
            self.assertRegex(option.id, menu_service.MENU_OPTION_ID_RE)
        sixth = options[5]
        self.assertEqual(sixth.id, "handover")
        self.assertEqual(sixth.label, "Talk to Staff")
        self.assertEqual(sixth.action, "handover")
        self.assertEqual(menu_service.ACTION_STEPS[sixth.action], "staff")

    def test_blank_option_six_row_with_stale_action_is_ignored(self):
        data = self._menu_form(self._default_options()[:5])
        # Old builder forms/cleared rows submitted the first action even when the
        # visible ID and label were blank. That must not make Option 6 invalid.
        data["option_action_5"] = "browse_products"
        response = self.section("menu", **data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        config = menu_service.menu_config(self.stored(self.tenant_id))
        self.assertEqual([option.id for option in config.options],
                         [option["id"] for option in self._default_options()[:5]])

    def test_new_option_six_derives_talk_to_staff_id_from_its_label(self):
        options = self._default_options()[:5]
        options.append({
            "id": "",
            "label": "Talk to Staff",
            "action": "handover",
            "enabled": True,
            "position": 6,
        })
        response = self.section("menu", **self._menu_form(options))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        config = menu_service.menu_config(self.stored(self.tenant_id))
        self.assertEqual(len(config.options), 6)
        self.assertEqual([option.id for option in config.options[:5]],
                         [option["id"] for option in self._default_options()[:5]])
        sixth = config.options[5]
        self.assertEqual(sixth.id, "talk_to_staff")
        self.assertEqual(sixth.label, "Talk to Staff")
        self.assertEqual(sixth.action, "handover")

    def test_new_options_get_unique_normalized_ids_from_labels(self):
        options = [
            {"id": "", "label": "Café & News!!", "action": "browse_products",
             "enabled": True, "position": 1},
            {"id": "", "label": "Café & News!!", "action": "faqs",
             "enabled": True, "position": 2},
        ]
        response = self.section("menu", **self._menu_form(options))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        config = menu_service.menu_config(self.stored(self.tenant_id))
        self.assertEqual([option.id for option in config.options],
                         ["cafe_news", "cafe_news_2"])
        for option in config.options:
            self.assertRegex(option.id, menu_service.MENU_OPTION_ID_RE)

    def test_builder_rejects_explicit_invalid_option_ids(self):
        before = self.stored(self.tenant_id)
        for invalid_id in ("Talk to Staff", "talk/staff", "talk.staff", "_talk"):
            data = self._menu_form([{
                "id": invalid_id,
                "label": "Talk to Staff",
                "action": "handover",
                "enabled": True,
                "position": 1,
            }])
            with self.subTest(invalid_id=invalid_id):
                response = self.section("menu", **data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(
                    "id must be lowercase letters, digits",
                    response.get_data(as_text=True),
                )
                self.assertEqual(self.stored(self.tenant_id), before)

    def test_builder_change_is_visible_in_simulator_without_restart(self):
        self.assertEqual(self.section("menu", **{
            "present_menu_enabled": "1",
            "menu_enabled": "1",
            "present_menu_options": "1",
            "menu_welcome_message": "Choose:",
            "option_id_0": "browse_products",
            "option_label_0": "First label",
            "option_action_0": "browse_products",
            "option_enabled_0": "1",
            "option_position_0": "1",
        }).status_code, 302)
        page = self.client.get(
            f"/playground/menu?tenant_id={self.tenant_id}"
        ).get_data(as_text=True)
        self.assertIn("First label", page)
        self.assertEqual(self.section("menu", **{
            "present_menu_enabled": "1",
            "menu_enabled": "1",
            "present_menu_options": "1",
            "menu_welcome_message": "Choose:",
            "option_id_0": "browse_products",
            "option_label_0": "Second label",
            "option_action_0": "browse_products",
            "option_enabled_0": "1",
            "option_position_0": "1",
        }).status_code, 302)
        refreshed = self.client.get(
            f"/playground/menu?tenant_id={self.tenant_id}"
        ).get_data(as_text=True)
        self.assertIn("Second label", refreshed)
        self.assertNotIn("First label", refreshed)


if __name__ == "__main__":
    unittest.main()

