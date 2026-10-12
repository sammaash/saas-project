"""Shared tenant-aware bot response engine.

The engine is intentionally pure: it loads tenant context and asks an AI provider for text,
but it has no WhatsApp, order, or payment adapters. Delivery and business actions belong to
channel-specific orchestration outside this module.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, Sequence

from .catalogue import CatalogueService
from .tenant_admin import tenant_admin

MAX_HISTORY_TURNS = 4
MAX_HISTORY_TURN_LENGTH = 1_000
MAX_RESPONSE_LENGTH = 8_000
_SENSITIVE_KEY_PARTS = (
    "access_key",
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)

logger = logging.getLogger("saas.bot")
PROVIDER_FAILURE_MESSAGE = "The AI provider could not generate a response. Try again."
_DIAGNOSTIC_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]{8,}\b", re.I),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|authorization|password|secret|token)\b"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"https?://[^\s]+", re.I),
)


class BotError(RuntimeError):
    """Base class for safe bot-engine failures."""


class BotConfigurationError(BotError):
    """The response provider is not configured."""


class BotContextError(BotError):
    """The selected tenant does not have usable bot context."""


class BotProviderError(BotError):
    """The external AI provider failed without exposing provider details."""


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str


@dataclass(frozen=True)
class TenantBotContext:
    tenant: dict[str, Any]
    settings: dict[str, Any]
    catalogue: list[dict[str, Any]]


@dataclass(frozen=True)
class BotResponseRequest:
    system_prompt: str
    history: tuple[ConversationTurn, ...]
    customer_message: str
    channel: str
    safe_test_mode: bool


@dataclass(frozen=True)
class BotResponse:
    text: str
    context: TenantBotContext


class BotResponseProvider(Protocol):
    def generate(self, request: BotResponseRequest) -> str:
        """Generate response text without performing delivery or business actions."""


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive_values(value: Any) -> Any:
    """Remove secret-like configuration before it reaches prompts or templates."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _is_sensitive_key(str(key)) else redact_sensitive_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    return value


class TenantBotContextLoader:
    """Load one tenant's context through the existing RLS-scoped data layer."""

    def __init__(self, database_url: str | None):
        self._database_url = database_url

    def load(self, tenant_id: str) -> TenantBotContext:
        started = time.perf_counter()
        with tenant_admin(tenant_id, self._database_url) as admin:
            tenant = admin.get_tenant()
            settings = admin.get_settings()
        if tenant is None or settings is None:
            raise BotContextError("The selected tenant does not have complete bot configuration.")

        catalogue = CatalogueService(tenant_id, self._database_url).list_active_products()
        safe_settings = {
            "timezone": settings.get("timezone"),
            "currency": settings.get("currency"),
            "business_rules": redact_sensitive_values(settings.get("business_rules") or {}),
            "bot_persona": redact_sensitive_values(settings.get("bot_persona") or {}),
            "business_hours": redact_sensitive_values(settings.get("business_hours") or {}),
            "configuration": redact_sensitive_values(settings.get("configuration") or {}),
        }
        safe_catalogue = redact_sensitive_values(catalogue)
        context = TenantBotContext(
            tenant={
                "id": str(tenant["id"]),
                "name": tenant["name"],
                "slug": tenant["slug"],
                "status": tenant["status"],
            },
            settings=safe_settings,
            catalogue=safe_catalogue,
        )
        logger.info(
            "Bot context timing duration_ms=%.1f products=%d variants=%d",
            (time.perf_counter() - started) * 1_000,
            len(context.catalogue),
            sum(len(product.get("variants") or []) for product in context.catalogue),
        )
        return context


class OpenAIChatProvider:
    """Minimal server-side OpenAI Chat Completions adapter."""

    _URL = "https://api.openai.com/v1/chat/completions"

    @classmethod
    def _sanitize_identifier(
        cls,
        value: Any,
        default: str,
        sensitive_values: Sequence[str],
    ) -> str:
        safe_value = cls._sanitize_message(value, sensitive_values)
        sanitized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", safe_value)[:80].strip("-")
        return sanitized or default

    @staticmethod
    def _sanitize_message(message: Any, sensitive_values: Sequence[str]) -> str:
        sanitized = str(message or "")
        for value in sorted(
            {item for item in sensitive_values if item},
            key=len,
            reverse=True,
        ):
            sanitized = sanitized.replace(value, "[redacted]")
        for pattern in _DIAGNOSTIC_SECRET_PATTERNS:
            sanitized = pattern.sub("[redacted]", sanitized)
        sanitized = "".join(
            character if character.isprintable() else " " for character in sanitized
        )
        return re.sub(r"\s+", " ", sanitized).strip()[:200] or "provider request failed"

    @staticmethod
    def _classify_error_message(
        error: BaseException,
        provider_message: str,
        status: int | None,
        parameter: str,
    ) -> str:
        """Return a useful fixed diagnostic without logging provider-supplied prose."""
        lowered = provider_message.lower()
        if isinstance(error, urllib.error.URLError) and not isinstance(
            error, urllib.error.HTTPError
        ):
            if "timed out" in lowered or "timeout" in lowered:
                return "provider connection timed out"
            return "provider network request failed"
        if isinstance(error, (TimeoutError, socket.timeout)):
            return "provider connection timed out"
        if isinstance(error, (TypeError, ValueError, KeyError, IndexError)):
            return "provider response was malformed"
        if parameter == "max_tokens":
            return "provider rejected max_tokens; model may require max_completion_tokens"
        if parameter == "max_completion_tokens":
            return "provider rejected max_completion_tokens"
        if "max_tokens" in lowered and "unsupported" in lowered:
            return "provider rejected max_tokens; model may require max_completion_tokens"
        if "max_completion_tokens" in lowered and "unsupported" in lowered:
            return "provider rejected max_completion_tokens"
        if "model" in lowered and any(
            phrase in lowered
            for phrase in ("does not exist", "not found", "not supported", "unavailable")
        ):
            return "configured model is unavailable or unsupported"
        if any(phrase in lowered for phrase in ("api key", "authentication", "unauthorized")):
            return "provider rejected authentication"
        if any(phrase in lowered for phrase in ("rate limit", "quota", "insufficient_quota")):
            return "provider rate limit or quota was exceeded"
        return f"provider returned HTTP {status}" if status is not None else "provider request failed"

    @classmethod
    def _safe_error_details(
        cls,
        error: BaseException,
        sensitive_values: Sequence[str],
    ) -> tuple[int | None, str, str, str]:
        status = None
        code = "unknown"
        error_type = type(error).__name__
        message = "provider request failed"
        parameter = ""
        if isinstance(error, urllib.error.HTTPError):
            status = getattr(error, "code", None)
            payload = (
                error.read(8_192).decode("utf-8", errors="replace")
                if getattr(error, "fp", None)
                else ""
            )
            try:
                detail = json.loads(payload)
            except (TypeError, ValueError):
                detail = {}
            error_obj = detail.get("error") if isinstance(detail, dict) else {}
            if isinstance(error_obj, dict):
                code = str(error_obj.get("code") or error_obj.get("type") or code)
                error_type = str(error_obj.get("type") or error_type)
                message = str(error_obj.get("message") or message)
                parameter = str(error_obj.get("param") or "")
            elif isinstance(error_obj, str):
                code = error_obj
        elif isinstance(error, urllib.error.URLError):
            code = "network_error"
            message = str(error.reason or "provider connection failed")
        elif isinstance(error, (TimeoutError, socket.timeout)):
            code = "timeout"
            message = "request timed out"
        elif isinstance(error, ValueError):
            code = "malformed_response"
            message = "response body could not be parsed"
        elif isinstance(error, (TypeError, KeyError, IndexError)):
            code = "malformed_response"
            message = "response payload missing expected fields"
        safe_message = cls._classify_error_message(error, message, status, parameter)
        return (
            status,
            cls._sanitize_identifier(code, "unknown", sensitive_values),
            cls._sanitize_identifier(error_type, "unknown", sensitive_values),
            safe_message,
        )

    def __init__(self, api_key: str, model: str, *, timeout: float = 30.0):
        if not api_key:
            raise BotConfigurationError("The AI provider is not configured.")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def _raise_provider_error(
        self,
        error: BaseException,
        request: BotResponseRequest,
    ) -> NoReturn:
        sensitive_values = (
            self._api_key,
            request.system_prompt,
            request.customer_message,
            *(turn.text for turn in request.history),
        )
        status, code, error_type, message = self._safe_error_details(error, sensitive_values)
        logger.error(
            "OpenAI provider failure status=%s code=%s type=%s message=%s",
            status,
            code,
            error_type,
            message,
        )
        raise BotProviderError(PROVIDER_FAILURE_MESSAGE) from error

    def _log_request_timing(
        self,
        started: float,
        api_started: float,
        response_processing_started: float | None,
        outcome: str,
    ) -> None:
        finished = time.perf_counter()
        if response_processing_started is None:
            api_ms = (finished - api_started) * 1_000
            response_processing_ms = 0.0
        else:
            api_ms = (response_processing_started - api_started) * 1_000
            response_processing_ms = (finished - response_processing_started) * 1_000
        logger.info(
            "OpenAI request timing total_ms=%.1f api_ms=%.1f "
            "response_processing_ms=%.1f model=%s outcome=%s",
            (finished - started) * 1_000,
            api_ms,
            response_processing_ms,
            self._sanitize_identifier(self._model, "unknown", (self._api_key,)),
            outcome,
        )

    def generate(self, request: BotResponseRequest) -> str:
        started = time.perf_counter()
        messages = [{"role": "system", "content": request.system_prompt}]
        messages.extend({"role": turn.role, "content": turn.text} for turn in request.history)
        messages.append({"role": "user", "content": request.customer_message})
        payload = {
            "model": self._model,
            "messages": messages,
            "max_completion_tokens": 800,
        }
        http_request = urllib.request.Request(
            self._URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        api_started = time.perf_counter()
        response_processing_started = None
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout) as response:  # noqa: S310
                response_body = response.read()
            response_processing_started = time.perf_counter()
            data = json.loads(response_body.decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as error:
            self._log_request_timing(
                started, api_started, response_processing_started, "http_error"
            )
            self._raise_provider_error(error, request)
        except urllib.error.URLError as error:
            self._log_request_timing(
                started, api_started, response_processing_started, "network_error"
            )
            self._raise_provider_error(error, request)
        except (TimeoutError, socket.timeout) as error:
            self._log_request_timing(
                started, api_started, response_processing_started, "timeout"
            )
            self._raise_provider_error(error, request)
        except (TypeError, ValueError, KeyError, IndexError) as error:
            self._log_request_timing(
                started, api_started, response_processing_started, "malformed_response"
            )
            self._raise_provider_error(error, request)
        if not isinstance(text, str) or not text.strip():
            self._log_request_timing(
                started, api_started, response_processing_started, "empty_response"
            )
            self._raise_provider_error(ValueError("empty provider response"), request)
        result = text.strip()[:MAX_RESPONSE_LENGTH]
        self._log_request_timing(
            started, api_started, response_processing_started, "success"
        )
        return result


class BotResponseEngine:
    """Generate grounded responses from tenant context and conversation history."""

    def __init__(self, context_loader: TenantBotContextLoader, provider: BotResponseProvider):
        self._context_loader = context_loader
        self._provider = provider

    @staticmethod
    def _history(turns: Sequence[ConversationTurn]) -> tuple[ConversationTurn, ...]:
        cleaned: list[ConversationTurn] = []
        for turn in turns[-MAX_HISTORY_TURNS:]:
            if turn.role not in {"user", "assistant"}:
                raise ValueError("Conversation roles must be user or assistant.")
            text = turn.text.strip()
            if text:
                cleaned.append(ConversationTurn(turn.role, text[:MAX_HISTORY_TURN_LENGTH]))
        return tuple(cleaned)

    @staticmethod
    def _prompt_catalogue(catalogue: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        compact = []
        for product in catalogue:
            item = {
                "name": product.get("name"),
                "description": product.get("description"),
                "category": product.get("category"),
                "attributes": BotResponseEngine._prompt_attributes(product.get("attributes")),
                "variants": [],
            }
            for variant in product.get("variants") or []:
                item["variants"].append(
                    {
                        "code": variant.get("code"),
                        "sku": variant.get("sku"),
                        "name": variant.get("name"),
                        "price": variant.get("price"),
                        "availability": variant.get("availability"),
                        "stock_quantity": variant.get("stock_quantity"),
                        "attributes": BotResponseEngine._prompt_attributes(
                            variant.get("attributes")
                        ),
                    }
                )
            compact.append(BotResponseEngine._without_empty(item))
        return compact

    @staticmethod
    def _prompt_attributes(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): item
            for key, item in value.items()
            if str(key).lower() not in {"image", "images", "image_url"} and item not in (None, "")
        }

    @staticmethod
    def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if item not in (None, "", {}, [])}

    @staticmethod
    def _system_prompt(context: TenantBotContext, *, safe_test_mode: bool) -> str:
        safety = (
            "This is an admin playground. Return text only. Do not claim to send messages, "
            "place orders, reserve inventory, charge or request payment, call tools, or perform "
            "any external action. Explain that such actions are disabled in test mode when asked."
            if safe_test_mode
            else "Return a customer-facing text response. External actions are handled elsewhere."
        )
        tenant_data = BotResponseEngine._without_empty(
            {
                "business_name": context.tenant.get("name"),
                "business_rules": context.settings["business_rules"],
                "bot_persona": context.settings["bot_persona"],
                "business_hours": context.settings["business_hours"],
                "configuration": context.settings["configuration"],
                "timezone": context.settings["timezone"],
                "currency": context.settings["currency"],
                "active_catalogue": BotResponseEngine._prompt_catalogue(context.catalogue),
            }
        )
        return (
            "You are the customer assistant for the selected tenant. Ground answers only in the "
            "tenant data below. Never use another tenant's data. If the answer is unavailable, say "
            "so rather than inventing it. Never describe OUT_OF_STOCK items as immediately "
            "available; describe MADE_TO_ORDER items clearly and do not invent a lead time. "
            "Follow the saved persona and business rules where they "
            "do not conflict with safety. "
            f"{safety}\n\nTENANT DATA (JSON):\n"
            f"{json.dumps(tenant_data, ensure_ascii=True, sort_keys=True, default=str, separators=(',', ':'))}"
        )

    def respond(
        self,
        tenant_id: str,
        customer_message: str,
        history: Sequence[ConversationTurn] = (),
        *,
        channel: str,
        safe_test_mode: bool,
        context: TenantBotContext | None = None,
    ) -> BotResponse:
        started = time.perf_counter()
        context_started = time.perf_counter()
        if context is None:
            context = self._context_loader.load(tenant_id)
        elif str(context.tenant.get("id")) != str(tenant_id):
            raise BotContextError("The supplied bot context does not match the selected tenant.")
        context_ms = (time.perf_counter() - context_started) * 1_000
        processing_started = time.perf_counter()
        request = BotResponseRequest(
            system_prompt=self._system_prompt(context, safe_test_mode=safe_test_mode),
            history=self._history(history),
            customer_message=customer_message,
            channel=channel,
            safe_test_mode=safe_test_mode,
        )
        request_processing_ms = (time.perf_counter() - processing_started) * 1_000
        provider_started = time.perf_counter()
        text = self._provider.generate(request)
        provider_ms = (time.perf_counter() - provider_started) * 1_000
        response_started = time.perf_counter()
        response = BotResponse(text=text, context=context)
        response_processing_ms = (time.perf_counter() - response_started) * 1_000
        logger.info(
            "Bot response timing total_ms=%.1f context_ms=%.1f request_processing_ms=%.1f "
            "provider_ms=%.1f response_processing_ms=%.1f prompt_chars=%d history_turns=%d "
            "products=%d variants=%d",
            (time.perf_counter() - started) * 1_000,
            context_ms,
            request_processing_ms,
            provider_ms,
            response_processing_ms,
            len(request.system_prompt),
            len(request.history),
            len(context.catalogue),
            sum(len(product.get("variants") or []) for product in context.catalogue),
        )
        return response
