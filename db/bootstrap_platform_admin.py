"""Register a platform administrator (operator tool; never run by the web service).

Usage:
    python db/bootstrap_platform_admin.py --user-id <verified Supabase Auth UUID> --confirm-verified

The UUID must be the immutable Auth user id of the intended administrator, confirmed by
signing in and checking that the verified JWT's ``sub`` equals it. Never invent or copy
an id from another project. Run with a short-lived operator (database owner) connection
supplied via SAAS_OPERATOR_DATABASE_URL or --database-url; it is never an application role.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("SAAS_OPERATOR_DATABASE_URL"))
    parser.add_argument(
        "--confirm-verified",
        action="store_true",
        help="Assert the UUID was verified against a real sign-in (JWT sub).",
    )
    args = parser.parse_args(argv)

    if not args.confirm_verified:
        parser.error("--confirm-verified is required: the UUID must be verified, not guessed.")
    if not args.database_url:
        parser.error("Provide --database-url or SAAS_OPERATOR_DATABASE_URL (operator connection).")
    try:
        user_id = str(uuid.UUID(args.user_id))
    except ValueError:
        parser.error("--user-id must be a valid UUID.")

    import psycopg  # noqa: PLC0415

    with psycopg.connect(args.database_url) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.platform_admins (user_id, status) values (%s, 'active') "
            "on conflict (user_id) do update set status = 'active' returning user_id",
            (user_id,),
        )
        print(f"platform administrator registered: {cursor.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
