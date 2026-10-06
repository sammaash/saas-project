"""Meta / WhatsApp provider interface.

This is an interface only. The real Meta MCP adapter must not be written until the MCP's
actual tools and authentication method have been verified. Until then the dashboard uses
:class:`UnavailableMetaProvider` (no calls possible) or the fake provider in tests.

Every operation takes a :class:`TenantMetaContext`, so each call names the tenant it acts
for, and a provider must refuse a ``credentials_ref`` that does not belong to that tenant.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class MetaNotConfigured(RuntimeError):
    """No Meta provider is available in this environment."""


class MetaError(RuntimeError):
    """A provider operation failed. Messages must never contain secrets."""


@dataclass(frozen=True)
class TenantMetaContext:
    tenant_id: str
    credentials_ref: str | None


@dataclass(frozen=True)
class BusinessInfo:
    business_id: str
    name: str


@dataclass(frozen=True)
class WabaInfo:
    waba_id: str
    name: str
    business_id: str


@dataclass(frozen=True)
class PhoneNumberInfo:
    phone_number_id: str
    waba_id: str
    display_phone_number: str
    verified_name: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class WebhookStatus:
    waba_id: str
    configured: bool
    callback_url: str | None = None


class MetaProvider(abc.ABC):
    @abc.abstractmethod
    def list_businesses(self, ctx: TenantMetaContext) -> list[BusinessInfo]: ...

    @abc.abstractmethod
    def list_wabas(self, ctx: TenantMetaContext, business_id: str) -> list[WabaInfo]: ...

    @abc.abstractmethod
    def list_phone_numbers(self, ctx: TenantMetaContext, waba_id: str) -> list[PhoneNumberInfo]: ...

    @abc.abstractmethod
    def get_waba(self, ctx: TenantMetaContext, waba_id: str) -> WabaInfo: ...

    @abc.abstractmethod
    def get_phone_number(self, ctx: TenantMetaContext, phone_number_id: str) -> PhoneNumberInfo: ...

    @abc.abstractmethod
    def register_number(
        self, ctx: TenantMetaContext, waba_id: str, display_phone_number: str
    ) -> PhoneNumberInfo: ...

    @abc.abstractmethod
    def check_webhook(self, ctx: TenantMetaContext, waba_id: str) -> WebhookStatus: ...


class UnavailableMetaProvider(MetaProvider):
    """Default provider: every operation reports that Meta is not configured."""

    def _fail(self, *_args, **_kwargs):
        raise MetaNotConfigured("Meta integration is not configured in this environment.")

    list_businesses = list_wabas = list_phone_numbers = _fail
    get_waba = get_phone_number = register_number = check_webhook = _fail
