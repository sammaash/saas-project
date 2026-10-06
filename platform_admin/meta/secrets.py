"""Secret resolution abstraction.

Channels store only a ``credentials_ref`` pointer (``tenants/<tenant_id>/<name>``). A
:class:`SecretResolver` turns a pointer into a secret inside the server process, at the
moment of use. No production secret store is bound yet; the default resolver fails closed.
"""

from __future__ import annotations

import abc
import uuid

SECRET_REF_PREFIX = "tenants/"


class SecretNotConfigured(RuntimeError):
    """No secret store is configured (or the reference has no value)."""


class CredentialScopeError(ValueError):
    """A credentials_ref does not belong to the tenant it was used for."""


class Secret:
    """Wraps a secret so it cannot be printed, logged or serialised by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("Secret cannot be serialised")


def tenant_credentials_ref(tenant_id: str, name: str = "whatsapp") -> str:
    """Build the pointer for a tenant. The tenant id is normalised through uuid."""
    return f"{SECRET_REF_PREFIX}{uuid.UUID(str(tenant_id))}/{name}"


def assert_ref_belongs_to_tenant(tenant_id: str, credentials_ref: str | None) -> str:
    """Return the ref if it is scoped to ``tenant_id``; raise otherwise."""
    expected_prefix = f"{SECRET_REF_PREFIX}{uuid.UUID(str(tenant_id))}/"
    if not credentials_ref or not credentials_ref.startswith(expected_prefix) or len(
        credentials_ref
    ) == len(expected_prefix):
        raise CredentialScopeError("credentials_ref does not belong to this tenant")
    return credentials_ref


class SecretResolver(abc.ABC):
    @abc.abstractmethod
    def resolve(self, credentials_ref: str) -> Secret:
        """Return the secret for a pointer, or raise :class:`SecretNotConfigured`."""

    def resolve_for_tenant(self, tenant_id: str, credentials_ref: str | None) -> Secret:
        """Resolve only after proving the pointer belongs to the tenant."""
        return self.resolve(assert_ref_belongs_to_tenant(tenant_id, credentials_ref))


class NullSecretResolver(SecretResolver):
    """Default: no secret store configured, so nothing can ever be resolved."""

    def resolve(self, credentials_ref: str) -> Secret:
        raise SecretNotConfigured("No secret store is configured for this environment.")


class StaticSecretResolver(SecretResolver):
    """In-memory resolver for tests and local development only."""

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)

    def resolve(self, credentials_ref: str) -> Secret:
        if credentials_ref not in self._values:
            raise SecretNotConfigured("No secret is stored for this reference.")
        return Secret(self._values[credentials_ref])
