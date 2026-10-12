"""Audit helper: redacts secrets from metadata before it reaches the audit log."""

from __future__ import annotations

import re
from typing import Any

from saas import platform

from ..config import Settings

_SECRET_KEY = re.compile(r"token|secret|password|passwd|authorization|api[_-]?key|bearer", re.I)
# credentials_ref is a pointer to a secret, not a secret, so it is allowed.
_ALLOWED_KEYS = frozenset({"credentials_ref"})
REDACTED = "[redacted]"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if _SECRET_KEY.search(str(key)) and key not in _ALLOWED_KEYS
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def record(
    settings: Settings,
    actor_user_id: str,
    tenant_id: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write a platform audit event through the platform function (verified actor).

    Call it inside the tenant transaction after the change: if the audit write fails the
    exception rolls the change back, so no change is committed without an audit event.
    """
    platform.record_audit(
        actor_user_id, tenant_id, action, entity_type, entity_id, redact(metadata or {}),
        url=settings.platform_database_url,
    )
