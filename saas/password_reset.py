"""Self-service password-reset gate (migration 0043).

``claim`` asks the database whether a reset email may be sent for an address. It returns
True only for an account the platform knows (``platform_user_directory``) that holds an
active membership of a draft or active tenant, and only while that address is under its
per-email hourly cap and the platform is under its global hourly cap. The caps are kept in
``public.password_reset_requests`` and serialized with an advisory lock, so they hold
across every application process. The caller learns nothing beyond the boolean and must
answer the user identically either way.
"""

from __future__ import annotations

from .db import connect


def claim(
    email: str,
    url: str | None = None,
    *,
    global_per_hour: int | None = None,
    per_email_per_hour: int | None = None,
) -> bool:
    """True when a reset email may be sent now (the request is then recorded)."""
    if not email or not isinstance(email, str):
        return False
    with connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select app.request_password_reset(%s, %s, %s)",
                (email, global_per_hour, per_email_per_hour),
            )
            row = cursor.fetchone()
        connection.commit()
    return bool(row and row[0])
