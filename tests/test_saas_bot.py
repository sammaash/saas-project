"""Unit tests for the shared tenant-aware bot response engine."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from saas.bot import (
    BotContextError,
    BotProviderError,
    BotResponseEngine,
    BotResponseRequest,
    ConversationTurn,
    OpenAIChatProvider,
    PROVIDER_FAILURE_MESSAGE,
    TenantBotContext,
    redact_sensitive_values,
)


class FakeLoader:
    def __init__(self, context: TenantBotContext):
        self.context = context
        self.tenant_ids: list[str] = []

    def load(self, tenant_id: str) -> TenantBotContext:
        self.tenant_ids.append(tenant_id)
        return self.context


class CapturingProvider:
    def __init__(self):
        self.requests: list[BotResponseRequest] = []

    def generate(self, request: BotResponseRequest) -> str:
        self.requests.append(request)
        return "provider-generated response"


class FakeHTTPResponse:
    def __init__(self, payload: bytes | None = None):
        self._payload = payload or json.dumps(
            {"choices": [{"message": {"content": "external provider response"}}]}
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


def _context() -> TenantBotContext:
    return TenantBotContext(
        tenant={"id": "tenant-a", "name": "SaaS Test", "slug": "saas-test", "status": "active"},
        settings={
            "timezone": "Africa/Nairobi",
            "currency": "KES",
            "business_rules": {"deposit_rate": 0.25},
            "bot_persona": {"tone": "friendly"},
            "business_hours": {"mon": "09:00-17:00"},
            "configuration": {"ordering": {"enabled": True}},
        },
        catalogue=[
            {
                "name": "Lemon cake",
                "variants": [{"name": "Large", "code": "large", "price": 500.0}],
            }
        ],
    )


class BotResponseEngineTests(unittest.TestCase):
    def test_provider_receives_catalogue_configuration_and_history(self):
        loader = FakeLoader(_context())
        provider = CapturingProvider()
        engine = BotResponseEngine(loader, provider)

        result = engine.respond(
            "tenant-a",
            "How much is a large lemon cake?",
            [ConversationTurn("user", "Hello"), ConversationTurn("assistant", "Hi")],
            channel="admin-playground",
            safe_test_mode=True,
        )

        self.assertEqual(result.text, "provider-generated response")
        self.assertEqual(loader.tenant_ids, ["tenant-a"])
        request = provider.requests[0]
        self.assertIn("Lemon cake", request.system_prompt)
        self.assertIn("deposit_rate", request.system_prompt)
        self.assertIn("friendly", request.system_prompt)
        self.assertIn("09:00-17:00", request.system_prompt)
        self.assertIn("ordering", request.system_prompt)
        self.assertEqual([turn.text for turn in request.history], ["Hello", "Hi"])

    def test_safe_mode_explicitly_disables_live_actions(self):
        provider = CapturingProvider()
        engine = BotResponseEngine(FakeLoader(_context()), provider)
        engine.respond(
            "tenant-a",
            "Place the order and charge me",
            channel="admin-playground",
            safe_test_mode=True,
        )
        prompt = provider.requests[0].system_prompt
        for phrase in ("Do not claim to send messages", "place orders", "charge", "call tools"):
            self.assertIn(phrase, prompt)

    def test_preloaded_context_avoids_a_redundant_context_load(self):
        context = _context()
        loader = FakeLoader(context)
        provider = CapturingProvider()
        result = BotResponseEngine(loader, provider).respond(
            "tenant-a",
            "Hello",
            channel="admin-playground",
            safe_test_mode=True,
            context=context,
        )
        self.assertEqual(result.context, context)
        self.assertEqual(loader.tenant_ids, [])

    def test_preloaded_context_must_match_the_selected_tenant(self):
        context = _context()
        provider = CapturingProvider()
        with self.assertRaisesRegex(BotContextError, "does not match"):
            BotResponseEngine(FakeLoader(context), provider).respond(
                "tenant-b",
                "Hello",
                channel="admin-playground",
                safe_test_mode=True,
                context=context,
            )
        self.assertEqual(provider.requests, [])

    def test_prompt_drops_internal_ids_and_images_but_keeps_customer_catalogue_facts(self):
        context = _context()
        context.catalogue[0].update(
            {
                "id": "internal-product-id",
                "description": "Fresh lemon cake",
                "attributes": {
                    "image_url": "https://private.invalid/product.jpg",
                    "serves": 12,
                },
            }
        )
        context.catalogue[0]["variants"][0].update(
            {"id": "internal-variant-id", "attributes": {"weight_kg": 2}}
        )
        provider = CapturingProvider()
        BotResponseEngine(FakeLoader(context), provider).respond(
            "tenant-a",
            "Tell me about the cake",
            channel="admin-playground",
            safe_test_mode=True,
        )
        prompt = provider.requests[0].system_prompt
        for expected in ("Lemon cake", "Fresh lemon cake", "Large", "500.0", "serves", "weight_kg"):
            self.assertIn(expected, prompt)
        for omitted in (
            "internal-product-id",
            "internal-variant-id",
            "private.invalid",
            '"slug"',
            '"status"',
        ):
            self.assertNotIn(omitted, prompt)

    def test_history_is_limited_to_four_recent_trimmed_turns(self):
        provider = CapturingProvider()
        history = [
            ConversationTurn("user" if index % 2 == 0 else "assistant", f"{index}-" + "x" * 1_200)
            for index in range(6)
        ]
        BotResponseEngine(FakeLoader(_context()), provider).respond(
            "tenant-a",
            "current",
            history,
            channel="admin-playground",
            safe_test_mode=True,
        )
        cleaned = provider.requests[0].history
        self.assertEqual([turn.text[:2] for turn in cleaned], ["2-", "3-", "4-", "5-"])
        self.assertTrue(all(len(turn.text) == 1_000 for turn in cleaned))

    def test_timing_logs_contain_only_aggregate_metadata(self):
        private_message = "private-customer-message"
        private_rule = "private-tenant-rule"
        context = _context()
        context.settings["business_rules"] = {"rule": private_rule}
        with self.assertLogs("saas.bot", level="INFO") as captured:
            BotResponseEngine(FakeLoader(context), CapturingProvider()).respond(
                "tenant-a",
                private_message,
                channel="admin-playground",
                safe_test_mode=True,
            )
        diagnostic = "\n".join(captured.output)
        for expected in (
            "total_ms=",
            "context_ms=",
            "provider_ms=",
            "response_processing_ms=",
            "prompt_chars=",
            "history_turns=",
            "products=",
            "variants=",
        ):
            self.assertIn(expected, diagnostic)
        self.assertNotIn(private_message, diagnostic)
        self.assertNotIn(private_rule, diagnostic)

    def test_secret_like_configuration_is_redacted_recursively(self):
        redacted = redact_sensitive_values(
            {
                "api_key": "hidden",
                "nested": {"access-token": "hidden", "ordinary": "visible"},
                "items": [{"password": "hidden"}],
            }
        )
        serialized = json.dumps(redacted)
        self.assertNotIn("hidden", serialized)
        self.assertIn("visible", serialized)
        self.assertEqual(redacted["api_key"], "[redacted]")

    def test_openai_provider_call_is_mocked_and_returns_text(self):
        provider = OpenAIChatProvider("test-key", "test-model")
        request = BotResponseRequest(
            system_prompt="system",
            history=(ConversationTurn("user", "earlier"),),
            customer_message="current",
            channel="admin-playground",
            safe_test_mode=True,
        )
        with self.assertLogs("saas.bot", level="INFO") as captured:
            with patch("saas.bot.urllib.request.urlopen", return_value=FakeHTTPResponse()) as urlopen:
                response = provider.generate(request)
        self.assertEqual(response, "external provider response")
        sent_request = urlopen.call_args.args[0]
        payload = json.loads(sent_request.data)
        self.assertEqual(sent_request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_completion_tokens"], 800)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertEqual([item["content"] for item in payload["messages"]], ["system", "earlier", "current"])
        diagnostic = "\n".join(captured.output)
        self.assertIn("OpenAI request timing total_ms=", diagnostic)
        self.assertIn("api_ms=", diagnostic)
        self.assertIn("response_processing_ms=", diagnostic)
        for sensitive in ("test-key", "system", "earlier", "current"):
            self.assertNotIn(sensitive, diagnostic)

    def test_openai_provider_logs_sanitized_http_diagnostics(self):
        request = BotResponseRequest(
            system_prompt="private tenant configuration",
            history=(ConversationTurn("user", "earlier private conversation"),),
            customer_message="current private conversation",
            channel="admin-playground",
            safe_test_mode=True,
        )
        provider = OpenAIChatProvider("test-key", "gpt-4o-mini")
        payload = json.dumps(
            {
                "error": {
                    "code": "unsupported_parameter",
                    "type": "invalid_request_error",
                    "param": "max_tokens",
                    "message": (
                        "Unsupported parameter: 'max_tokens' is not supported with this model. "
                        "Use 'max_completion_tokens' instead; current private conversation; "
                        "Authorization: Bearer test-key; https://example.invalid/?token=test-key"
                    ),
                }
            }
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(payload),
        )

        with self.assertLogs("saas.bot", level="ERROR") as captured:
            with patch("saas.bot.urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(BotProviderError) as raised:
                    provider.generate(request)
        self.assertEqual(str(raised.exception), PROVIDER_FAILURE_MESSAGE)

        diagnostic = "\n".join(captured.output)
        self.assertIn("status=400", diagnostic)
        self.assertIn("code=unsupported_parameter", diagnostic)
        self.assertIn("type=invalid_request_error", diagnostic)
        self.assertIn(
            "message=provider rejected max_tokens; model may require max_completion_tokens",
            diagnostic,
        )
        for sensitive in (
            "test-key",
            "current private conversation",
            "earlier private conversation",
            "private tenant configuration",
            "Bearer",
            "example.invalid",
        ):
            self.assertNotIn(sensitive, diagnostic)

    def test_openai_provider_logs_sanitized_network_error(self):
        request = BotResponseRequest(
            system_prompt="system",
            history=(),
            customer_message="private customer message",
            channel="admin-playground",
            safe_test_mode=True,
        )
        provider = OpenAIChatProvider("test-key", "gpt-4o-mini")

        reason = "failed at https://test-key@example.invalid private customer message"
        with self.assertLogs("saas.bot", level="ERROR") as captured:
            with patch(
                "saas.bot.urllib.request.urlopen",
                side_effect=urllib.error.URLError(reason),
            ):
                with self.assertRaises(BotProviderError) as raised:
                    provider.generate(request)
        self.assertEqual(str(raised.exception), PROVIDER_FAILURE_MESSAGE)
        diagnostic = "\n".join(captured.output)
        self.assertIn("status=None", diagnostic)
        self.assertIn("code=network_error", diagnostic)
        self.assertIn("type=URLError", diagnostic)
        self.assertIn("message=provider network request failed", diagnostic)
        self.assertNotIn("test-key", diagnostic)
        self.assertNotIn("private customer message", diagnostic)
        self.assertNotIn("example.invalid", diagnostic)

    def test_openai_provider_logs_malformed_response_without_body(self):
        request = BotResponseRequest(
            system_prompt="system",
            history=(),
            customer_message="current",
            channel="admin-playground",
            safe_test_mode=True,
        )
        provider = OpenAIChatProvider("test-key", "gpt-4o-mini")

        with self.assertLogs("saas.bot", level="ERROR") as captured:
            with patch(
                "saas.bot.urllib.request.urlopen",
                return_value=FakeHTTPResponse(b'{"unexpected":"provider-body-secret"}'),
            ):
                with self.assertRaisesRegex(BotProviderError, "could not generate"):
                    provider.generate(request)
        diagnostic = "\n".join(captured.output)
        self.assertIn("code=malformed_response", diagnostic)
        self.assertIn("type=KeyError", diagnostic)
        self.assertNotIn("provider-body-secret", diagnostic)

    def test_openai_provider_handles_successful_response(self):
        request = BotResponseRequest(
            system_prompt="system",
            history=(),
            customer_message="current",
            channel="admin-playground",
            safe_test_mode=True,
        )
        provider = OpenAIChatProvider("test-key", "gpt-4o-mini")

        with patch("saas.bot.urllib.request.urlopen", return_value=FakeHTTPResponse()):
            self.assertEqual(provider.generate(request), "external provider response")


if __name__ == "__main__":
    unittest.main()
