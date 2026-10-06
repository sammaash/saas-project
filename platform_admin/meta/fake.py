"""Fake Meta provider for tests and UI development. Makes no network calls."""

from __future__ import annotations

from dataclasses import dataclass, field

from .provider import (
    BusinessInfo,
    MetaError,
    MetaProvider,
    PhoneNumberInfo,
    TenantMetaContext,
    WabaInfo,
    WebhookStatus,
)
from .secrets import SecretResolver


@dataclass
class FakeAccount:
    """What one credentials_ref can see on the fake Meta side."""

    businesses: list[BusinessInfo] = field(default_factory=list)
    wabas: list[WabaInfo] = field(default_factory=list)
    phone_numbers: list[PhoneNumberInfo] = field(default_factory=list)
    webhooks: dict[str, WebhookStatus] = field(default_factory=dict)


class FakeMetaProvider(MetaProvider):
    def __init__(self, accounts: dict[str, FakeAccount], secrets: SecretResolver):
        self._accounts = accounts
        self._secrets = secrets
        self.calls: list[tuple[str, str]] = []  # (operation, tenant_id)

    def _account(self, operation: str, ctx: TenantMetaContext) -> FakeAccount:
        self.calls.append((operation, ctx.tenant_id))
        # Proves the pointer is this tenant's and that a secret exists, as a real adapter must.
        self._secrets.resolve_for_tenant(ctx.tenant_id, ctx.credentials_ref)
        account = self._accounts.get(ctx.credentials_ref or "")
        if account is None:
            raise MetaError("No Meta account is available for this credential reference.")
        return account

    def list_businesses(self, ctx):
        return list(self._account("list_businesses", ctx).businesses)

    def list_wabas(self, ctx, business_id):
        return [w for w in self._account("list_wabas", ctx).wabas if w.business_id == business_id]

    def list_phone_numbers(self, ctx, waba_id):
        return [
            p for p in self._account("list_phone_numbers", ctx).phone_numbers if p.waba_id == waba_id
        ]

    def get_waba(self, ctx, waba_id):
        for waba in self._account("get_waba", ctx).wabas:
            if waba.waba_id == waba_id:
                return waba
        raise MetaError("WABA not found.")

    def get_phone_number(self, ctx, phone_number_id):
        for number in self._account("get_phone_number", ctx).phone_numbers:
            if number.phone_number_id == phone_number_id:
                return number
        raise MetaError("Phone number not found.")

    def register_number(self, ctx, waba_id, display_phone_number):
        account = self._account("register_number", ctx)
        number = PhoneNumberInfo(
            phone_number_id=f"fake-{len(account.phone_numbers) + 1}",
            waba_id=waba_id,
            display_phone_number=display_phone_number,
            status="PENDING",
        )
        account.phone_numbers.append(number)
        return number

    def check_webhook(self, ctx, waba_id):
        account = self._account("check_webhook", ctx)
        return account.webhooks.get(waba_id, WebhookStatus(waba_id=waba_id, configured=False))
