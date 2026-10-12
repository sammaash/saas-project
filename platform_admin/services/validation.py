"""Input validation and error translation shared by the services."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator


class ServiceError(Exception):
    """A user-correctable failure. ``message`` is safe to display."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")
INT_MAX = 2_147_483_647


def text(value: Any, field: str, *, max_length: int = 200, required: bool = True) -> str | None:
    cleaned = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    if "\x00" in cleaned:
        raise ServiceError(f"{field} contains an invalid character.")
    if not cleaned:
        if required:
            raise ServiceError(f"{field} is required.")
        return None
    if len(cleaned) > max_length:
        raise ServiceError(f"{field} must be at most {max_length} characters.")
    return cleaned


def slug(value: Any) -> str:
    cleaned = text(value, "Slug", max_length=40) or ""
    if not SLUG_RE.match(cleaned):
        raise ServiceError("Slug must be 3-40 characters: lowercase letters, digits and hyphens.")
    return cleaned


def currency(value: Any) -> str:
    cleaned = (text(value, "Currency", max_length=3) or "").upper()
    if not CURRENCY_RE.match(cleaned):
        raise ServiceError("Currency must be a 3-letter ISO code.")
    return cleaned


def timezone(value: Any) -> str:
    cleaned = text(value, "Timezone", max_length=64) or ""
    if not TIMEZONE_RE.match(cleaned):
        raise ServiceError("Timezone must be an IANA name such as Africa/Nairobi.")
    try:
        import zoneinfo  # noqa: PLC0415

        known = zoneinfo.available_timezones()
        if known and cleaned not in known:
            raise ServiceError("Unknown timezone.")
    except ImportError:  # pragma: no cover
        pass
    return cleaned


def uuid_value(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise ServiceError(f"{field} must be a valid UUID.") from None


def optional_uuid(value: Any, field: str) -> str | None:
    return uuid_value(value, field) if value and str(value).strip() else None


def json_object(value: Any, field: str) -> dict[str, Any]:
    raw = (value or "").strip() if isinstance(value, str) or value is None else value
    if raw in ("", None):
        return {}
    if isinstance(raw, str) and ("\x00" in raw or "\\u0000" in raw.lower()):
        raise ServiceError(f"{field} contains an invalid character.")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise ServiceError(f"{field} must be valid JSON.") from None
    if not isinstance(parsed, dict):
        raise ServiceError(f"{field} must be a JSON object.")
    return parsed


def price(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ServiceError("Price must be a number.") from None
    if not amount.is_finite() or amount < 0 or amount >= Decimal("10000000000"):
        raise ServiceError("Price must be between 0 and 9,999,999,999.99.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ServiceError("Price can have at most 2 decimal places.")
    return amount.quantize(Decimal("0.01"))


def integer(value: Any, field: str, *, minimum: int = 0, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ServiceError(f"{field} must be a whole number.") from None
    if number < minimum or number > INT_MAX:
        raise ServiceError(f"{field} must be between {minimum} and {INT_MAX}.")
    return number


def translate_db_error(error: Exception) -> ServiceError | None:
    """Map expected database constraint failures to user-safe messages; else None."""
    try:
        import psycopg.errors as pg  # noqa: PLC0415
    except ModuleNotFoundError:  # pragma: no cover
        return None
    constraint = getattr(getattr(error, "diag", None), "constraint_name", "") or ""
    if isinstance(error, pg.UniqueViolation):
        if "phone_number_id" in constraint:
            return ServiceError("That WhatsApp phone number is already assigned to a channel.", 409)
        if "slug" in constraint:
            return ServiceError("That slug is already in use.", 409)
        return ServiceError("That value already exists.", 409)
    if isinstance(error, pg.ForeignKeyViolation):
        return ServiceError("A referenced record does not exist for this tenant.", 400)
    if isinstance(error, pg.CheckViolation):
        message = getattr(getattr(error, "diag", None), "message_primary", "") or ""
        if message.startswith("tenant is not ready to activate"):
            return ServiceError(message[0].upper() + message[1:] + ".", 409)
        if message.startswith("an active channel requires"):
            return ServiceError("An active channel requires an active tenant.", 409)
        if message.startswith("tenant must retain"):
            return ServiceError("A tenant must keep at least one active owner.", 409)
        if message.startswith(("this phone number is reserved", "disable the channel",
                               "a channel cannot be moved")):
            return ServiceError(message[0].upper() + message[1:] + ".", 409)
        return ServiceError("A value failed validation.", 400)
    if isinstance(error, pg.InsufficientPrivilege):
        return ServiceError("Not permitted.", 403)
    if isinstance(error, pg.NoDataFound):
        message = getattr(getattr(error, "diag", None), "message_primary", "") or "not found"
        return ServiceError(message[0].upper() + message[1:] + ".", 404)
    if isinstance(error, pg.InvalidParameterValue):
        message = getattr(getattr(error, "diag", None), "message_primary", "") or "invalid value"
        return ServiceError(message[0].upper() + message[1:] + ".", 400)
    if isinstance(error, pg.DataError):
        return ServiceError("A value is invalid or out of range.", 400)
    return None


@contextmanager
def translated() -> Iterator[None]:
    """Convert expected database failures into ServiceError; re-raise anything else."""
    try:
        yield
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001
        friendly = translate_db_error(error)
        if friendly is None:
            raise
        raise friendly from None
