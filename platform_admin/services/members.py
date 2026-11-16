"""Tenant onboarding and member management for platform administrators.

Identity lives in Supabase Auth and membership in ``public.tenant_users``. This module
joins them without ever creating a second account for one email:

* ``onboard`` validates everything and checks the slug *before* touching Supabase, then
  invites (or finds) the owner's account, then provisions the tenant, its settings, the
  active owner membership and the audit trail in ONE database transaction that reuses
  ``app.platform_provision_tenant``.
* If provisioning fails after an invitation was sent, nothing is deleted. The admin is told
  that the account exists and to resubmit with "Link an existing account", which finds the
  same account by email: retries converge instead of duplicating.
* Every membership change goes through an ``app.platform_*`` function that requires a
  platform administrator, checks the membership belongs to the tenant in the URL, keeps the
  final-active-owner rule and writes the audit row in the same transaction.

Passwords are never handled here: owners set their own through the invitation link.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from saas import platform

from ..auth_admin import AuthAdmin, AuthAdminError, AuthUser, auth_callback_url, normalize_email
from ..config import Settings
from . import validation as v
from .tenants import phone_value

logger = logging.getLogger("platform_admin.members")

MEMBER_ROLES = ("owner", "staff")
MEMBER_STATUSES = ("invited", "active", "disabled")
NEW_MEMBER_STATUSES = ("active", "invited")
OWNER_MODES = ("invite", "link")
#: Members page views: every membership, memberships whose account the platform has not
#: verified (placeholders, legacy UUIDs), and verified accounts that hold no membership.
MEMBER_VIEWS = ("all", "unverified", "unattached")


@dataclass(frozen=True)
class OnboardingResult:
    tenant_id: str
    owner_user_id: str
    owner_email: str
    invited: bool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _email(value: Any, field: str = "Email") -> str:
    try:
        return normalize_email(value)
    except AuthAdminError:
        raise v.ServiceError(f"{field} must be a valid email address.") from None


def _optional_email(value: Any, field: str) -> str | None:
    return _email(value, field) if value and str(value).strip() else None


def _choice(value: Any, field: str, allowed: tuple[str, ...], default: str | None = None) -> str:
    cleaned = (str(value).strip().lower() if value is not None else "") or (default or "")
    if cleaned not in allowed:
        raise v.ServiceError(f"{field} must be one of: {', '.join(allowed)}.")
    return cleaned


def _optional_choice(value: Any, field: str, allowed: tuple[str, ...]) -> str | None:
    return _choice(value, field, allowed) if value and str(value).strip() else None


def _service_error(error: AuthAdminError) -> v.ServiceError:
    status = {"email_exists": 409, "invalid_email": 400, "not_configured": 503,
              "user_not_found": 404, "rate_limited": 429}.get(error.code, 502)
    return v.ServiceError(str(error), status)


def _callback(settings: Settings) -> str:
    if not settings.public_url:
        raise v.ServiceError(
            "Invitations are not configured: set PLATFORM_PUBLIC_URL in the server environment.", 503
        )
    return auth_callback_url(settings.public_url)


def _verified_account(auth_admin: AuthAdmin, email: str, user_id: str | None) -> AuthUser:
    """The existing Supabase account for ``email`` (and ``user_id`` when given), or an error."""
    try:
        if user_id:
            account = auth_admin.get_user(user_id)
            if account is None:
                raise v.ServiceError("No Supabase Auth account has that user id.", 404)
            if account.email != email:
                raise v.ServiceError(
                    "That user id belongs to a different email address. Check both values.", 409
                )
            return account
        account = auth_admin.find_user_by_email(email)
    except AuthAdminError as error:
        raise _service_error(error) from None
    if account is None:
        raise v.ServiceError(
            f"No Supabase Auth account uses {email}. Choose \"Invite\" to create one.", 404
        )
    return account


def _invite_new_account(
    settings: Settings, auth_admin: AuthAdmin, email: str, full_name: str | None
) -> AuthUser:
    """Invite ``email``; refuse (without sending anything) when an account already exists."""
    redirect_to = _callback(settings)
    try:
        if auth_admin.find_user_by_email(email) is not None:
            raise v.ServiceError(
                f"An account already exists for {email}, so no invitation was sent. "
                "Choose \"Link an existing account\" to use it.",
                409,
            )
        return auth_admin.invite(email, full_name, redirect_to)
    except AuthAdminError as error:
        if error.code == "email_exists":
            raise v.ServiceError(
                f"An account already exists for {email}, so no invitation was sent. "
                "Choose \"Link an existing account\" to use it.",
                409,
            ) from None
        raise _service_error(error) from None


def _remember_invited_account(
    settings: Settings, admin_id: str, account: AuthUser, full_name: str | None
) -> None:
    """Record a just-invited account in the directory in its OWN committed transaction.

    The provisioning transaction that follows may still fail; the invited account must
    stay visible (Members -> "Accounts without a membership") so it can be linked instead
    of being invited again. A failure here is logged and never blocks the onboarding: the
    provisioning transaction writes the same row.
    """
    try:
        platform.upsert_user_directory(
            admin_id, account.id, account.email, full_name, True,
            url=settings.platform_database_url,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record the invited account in the directory")


def _recovery_message(error_message: str, email: str, invited: bool, what: str) -> str:
    account = "was created and the invitation email was sent" if invited else "is unchanged"
    return (
        f"{error_message} The {what} was not saved. The Auth account for {email} {account}; "
        "nothing was deleted. Fix the problem and submit again with "
        "\"Link an existing account\" and the same email."
    )


# ---------------------------------------------------------------------------
# onboarding
# ---------------------------------------------------------------------------
def onboard(
    settings: Settings, auth_admin: AuthAdmin, admin_id: str, form: dict[str, Any]
) -> OnboardingResult:
    # 1. Validate everything before any external call.
    name = v.text(form.get("name"), "Business name")
    slug = v.slug(form.get("slug"))
    timezone = v.timezone(form.get("timezone") or "Africa/Nairobi")
    currency = v.currency(form.get("currency") or "KES")
    plan_id = v.optional_uuid(form.get("plan_id"), "Plan")
    contact_email = _optional_email(form.get("contact_email"), "Contact email")
    contact_phone = phone_value(form.get("contact_phone"), "Business phone number")
    owner_name = v.text(form.get("owner_full_name"), "Owner's full name", max_length=200)
    owner_email = _email(form.get("owner_email"), "Owner's email")
    mode = _choice(form.get("owner_mode"), "Owner account", OWNER_MODES, default="invite")
    owner_user_id = v.optional_uuid(form.get("owner_user_id"), "Existing user id")

    # 2. Duplicate tenant: refuse before any account is created or email is sent.
    with v.translated():
        existing = platform.list_tenants(admin_id, url=settings.platform_database_url)
    if any(tenant["slug"] == slug for tenant in existing):
        raise v.ServiceError(
            f"A tenant with the slug \"{slug}\" already exists. Open it from Tenants instead "
            "of creating another one.",
            409,
        )

    # 3. Resolve the owner's Supabase account (never a duplicate).
    if mode == "link":
        account, invited = _verified_account(auth_admin, owner_email, owner_user_id), False
    else:
        account, invited = _invite_new_account(settings, auth_admin, owner_email, owner_name), True
        _remember_invited_account(settings, admin_id, account, owner_name)

    # 4. Provision atomically with the real Auth UUID.
    try:
        with v.translated():
            tenant_id = platform.onboard_tenant(
                admin_id,
                name=name, slug=slug, timezone=timezone, currency=currency,
                owner_user_id=account.id, owner_email=account.email,
                owner_full_name=owner_name, owner_invited=invited,
                initial_plan_id=plan_id,
                contact_email=contact_email, contact_phone=contact_phone,
                url=settings.platform_database_url,
            )
    except v.ServiceError as error:
        raise v.ServiceError(
            _recovery_message(error.message, account.email, invited, "tenant"), error.status
        ) from None
    except Exception:  # noqa: BLE001 - database unreachable etc.: report, never half-delete
        logger.exception("onboarding provisioning failed after the owner account was resolved")
        raise v.ServiceError(
            _recovery_message("The database could not be reached.", account.email, invited, "tenant"),
            503,
        ) from None
    return OnboardingResult(tenant_id, account.id, account.email, invited)


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------
def list_members(settings: Settings, admin_id: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
    tenant_id = v.optional_uuid(filters.get("tenant_id"), "Tenant")
    role = _optional_choice(filters.get("role"), "Role", MEMBER_ROLES)
    status = _optional_choice(filters.get("status"), "Status", MEMBER_STATUSES)
    search = v.text(filters.get("q"), "Search", max_length=200, required=False)
    with v.translated():
        rows = platform.list_members(
            admin_id, tenant_id, role, status, search, url=settings.platform_database_url
        )
    for row in rows:
        for key in ("membership_id", "tenant_id", "user_id"):
            row[key] = str(row[key])
    return rows


def _membership(settings: Settings, admin_id: str, tenant_id: str, membership_id: str) -> dict[str, Any]:
    membership_id = v.uuid_value(membership_id, "Membership")
    for row in list_members(settings, admin_id, {"tenant_id": tenant_id}):
        if row["membership_id"] == membership_id:
            return row
    raise v.ServiceError("Membership not found.", 404)


def add_existing_member(
    settings: Settings, auth_admin: AuthAdmin, admin_id: str, tenant_id: str, form: dict[str, Any]
) -> str:
    """Link an existing Supabase account to the tenant. Returns a confirmation message."""
    role = _choice(form.get("role"), "Role", MEMBER_ROLES)
    status = _choice(form.get("status"), "Status", NEW_MEMBER_STATUSES, default="active")
    full_name = v.text(form.get("full_name"), "Full name", max_length=200, required=False)
    email = _optional_email(form.get("email"), "Email")
    user_id = v.optional_uuid(form.get("user_id"), "User id")
    if not email and not user_id:
        raise v.ServiceError("Enter the account's email (or its user id).")

    # Every membership must point at a real Supabase account. Without the service key an
    # account cannot be verified, so nothing is added (no "phantom" members or owners).
    if not settings.auth_admin_configured:
        raise v.ServiceError(
            "Supabase Auth administration is not configured on this server "
            "(SUPABASE_SERVICE_ROLE_KEY and PLATFORM_PUBLIC_URL), so the account cannot be "
            "verified and nothing was added.",
            503,
        )
    if email:
        account = _verified_account(auth_admin, email, user_id)
    else:
        try:
            account = auth_admin.get_user(user_id)
        except AuthAdminError as error:
            raise _service_error(error) from None
        if account is None:
            raise v.ServiceError("No Supabase Auth account has that user id.", 404)
    user_id, email = account.id, account.email
    full_name = full_name or account.full_name

    with v.translated():
        platform.add_member(
            admin_id, tenant_id, user_id, role, status,
            email=email, full_name=full_name, url=settings.platform_database_url,
        )
    return f"Added {email} as {role}."


def invite_member(
    settings: Settings, auth_admin: AuthAdmin, admin_id: str, tenant_id: str, form: dict[str, Any]
) -> str:
    """Invite a NEW person by email and add them to the tenant."""
    role = _choice(form.get("role"), "Role", MEMBER_ROLES)
    full_name = v.text(form.get("full_name"), "Full name", max_length=200)
    email = _email(form.get("email"))
    # Confirm the tenant exists before sending anything.
    with v.translated():
        if platform.get_tenant(admin_id, tenant_id, url=settings.platform_database_url) is None:
            raise v.ServiceError("Tenant not found.", 404)
    account = _invite_new_account(settings, auth_admin, email, full_name)
    _remember_invited_account(settings, admin_id, account, full_name)
    try:
        with v.translated():
            platform.add_member(
                admin_id, tenant_id, account.id, role, "active",
                email=account.email, full_name=full_name, invited=True,
                url=settings.platform_database_url,
            )
    except v.ServiceError as error:
        raise v.ServiceError(
            _recovery_message(error.message, account.email, True, "membership"), error.status
        ) from None
    return f"Invitation sent to {account.email}; they were added as {role}."


def change_role(settings: Settings, admin_id: str, tenant_id: str, membership_id: str, role: Any) -> str:
    membership_id = v.uuid_value(membership_id, "Membership")
    role = _choice(role, "Role", MEMBER_ROLES)
    with v.translated():
        platform.update_member(admin_id, tenant_id, membership_id, role=role,
                               url=settings.platform_database_url)
    return f"Role changed to {role}."


def set_status(settings: Settings, admin_id: str, tenant_id: str, membership_id: str, status: Any) -> str:
    membership_id = v.uuid_value(membership_id, "Membership")
    status = _choice(status, "Status", ("active", "disabled"))
    with v.translated():
        platform.update_member(admin_id, tenant_id, membership_id, status=status,
                               url=settings.platform_database_url)
    return "Membership activated." if status == "active" else "Membership deactivated."


def remove(settings: Settings, admin_id: str, tenant_id: str, membership_id: str) -> str:
    membership_id = v.uuid_value(membership_id, "Membership")
    with v.translated():
        platform.remove_member(admin_id, tenant_id, membership_id, url=settings.platform_database_url)
    return "Tenant access revoked. The person's Auth account was not deleted."


def _audit_member_event(
    settings: Settings, admin_id: str, tenant_id: str, member: dict[str, Any], action: str
) -> None:
    """Audit an action that has no database write of its own (ids only, no email)."""
    with v.translated():
        platform.record_audit(
            admin_id, tenant_id, action, "tenant_user", member["membership_id"],
            {"user_id": member["user_id"]}, url=settings.platform_database_url,
        )


def resend_invitation(
    settings: Settings, auth_admin: AuthAdmin, admin_id: str, tenant_id: str, membership_id: str
) -> str:
    member = _membership(settings, admin_id, tenant_id, membership_id)
    if not member.get("email"):
        raise v.ServiceError("This member has no email on record; add them again by email.", 409)
    try:
        account = auth_admin.invite(member["email"], member.get("full_name"), _callback(settings))
    except AuthAdminError as error:
        if error.code == "email_exists":
            raise v.ServiceError(
                "This person has already accepted an invitation. Send a password reset instead.", 409
            ) from None
        raise _service_error(error) from None
    _remember_invited_account(settings, admin_id, account, member.get("full_name"))
    _audit_member_event(settings, admin_id, tenant_id, member, "member.invitation_resent")
    if account.id != member["user_id"]:
        # The original account was deleted in Supabase and the invitation created a new
        # one. The membership still points at the old id, so say exactly what to do.
        return (
            f"Invitation sent to {member['email']}, but it created a NEW account: the one this "
            "membership points to no longer exists. Add the new account from \"Accounts without "
            "a membership\" and revoke this membership."
        )
    return f"Invitation re-sent to {member['email']}."


def send_password_reset(
    settings: Settings, auth_admin: AuthAdmin, admin_id: str, tenant_id: str, membership_id: str
) -> str:
    member = _membership(settings, admin_id, tenant_id, membership_id)
    if not member.get("email"):
        raise v.ServiceError("This member has no email on record; add them again by email.", 409)
    try:
        auth_admin.send_password_reset(member["email"], _callback(settings))
    except AuthAdminError as error:
        raise _service_error(error) from None
    _audit_member_event(settings, admin_id, tenant_id, member, "member.password_reset_sent")
    return f"Password reset email sent to {member['email']}."


def list_unattached_accounts(settings: Settings, admin_id: str, search: Any = None) -> list[dict[str, Any]]:
    """Accounts the platform invited or linked that hold no membership at all."""
    search = v.text(search, "Search", max_length=200, required=False)
    with v.translated():
        rows = platform.list_unattached_accounts(admin_id, search, url=settings.platform_database_url)
    for row in rows:
        row["user_id"] = str(row["user_id"])
    return rows
