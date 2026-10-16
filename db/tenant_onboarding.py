"""Operator CLI: load a tenant's catalogue and configuration without the dashboard.

This is a headless front end for the *same* service layer the dashboard uses
(``platform_admin.services.catalogue_import`` and ``platform_admin.services.tenants``),
so validation, auditing, catalogue versioning, RLS scoping and readiness checks behave
exactly as they do in the UI. It exists because a first onboarding often has to happen
from an operator machine where the dashboard cannot be driven interactively.

Nothing here is tenant-specific: every command takes ``--tenant-slug`` and reads a file
from ``db/tenants/<slug>/``. Adding a tenant is adding data, never code.

Usage (the environment must carry SAAS_DATABASE_URL, SAAS_PLATFORM_DATABASE_URL and
PLATFORM_SECRET_KEY; ``--env-file`` loads them from a file when they are not exported)::

    python db/tenant_onboarding.py catalogue --tenant-slug cindy-bakes \
        --csv db/tenants/cindy-bakes/catalogue.csv
    python db/tenant_onboarding.py settings  --tenant-slug cindy-bakes \
        --json db/tenants/cindy-bakes/settings.json
    python db/tenant_onboarding.py verify    --tenant-slug cindy-bakes
    python db/tenant_onboarding.py status    --tenant-slug cindy-bakes --set active

Every write needs ``--admin-user-id`` (or ``SAAS_PLATFORM_ADMIN_USER_ID``): the database
re-checks that the identity is an active platform administrator before it records the
audit event, so a change cannot commit without a verified actor.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # allow `python db/tenant_onboarding.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from platform_admin.config import Settings  # noqa: E402
from platform_admin.services import catalogue_import, tenants  # noqa: E402
from platform_admin.services.validation import ServiceError  # noqa: E402
from saas.tenant_admin import tenant_admin  # noqa: E402


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader: no interpolation, an existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _settings(env_file: Path) -> Settings:
    _load_env_file(env_file)
    missing = [
        name
        for name in ("SAAS_DATABASE_URL", "SAAS_PLATFORM_DATABASE_URL", "PLATFORM_SECRET_KEY")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise SystemExit(f"Missing required configuration: {', '.join(missing)}")
    return Settings(
        secret_key=os.environ["PLATFORM_SECRET_KEY"],
        app_database_url=os.environ["SAAS_DATABASE_URL"],
        platform_database_url=os.environ["SAAS_PLATFORM_DATABASE_URL"],
        allow_active_channels=os.environ.get("PLATFORM_ALLOW_ACTIVE_CHANNELS", "0") == "1",
    )


def _admin_id(args: argparse.Namespace) -> str:
    admin_id = args.admin_user_id or os.environ.get("SAAS_PLATFORM_ADMIN_USER_ID", "")
    if not admin_id.strip():
        raise SystemExit(
            "An administrator identity is required: pass --admin-user-id or set "
            "SAAS_PLATFORM_ADMIN_USER_ID. The database verifies it before auditing."
        )
    return admin_id.strip()


def _tenant(settings: Settings, admin_id: str, slug: str) -> dict:
    matches = [row for row in tenants.list_tenants(settings, admin_id) if row["slug"] == slug]
    if not matches:
        raise SystemExit(f"No tenant has the slug {slug!r}.")
    if len(matches) > 1:
        raise SystemExit(f"Slug {slug!r} is ambiguous: {len(matches)} tenants match.")
    return matches[0]


def _resolve(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else REPO_ROOT / path


def _workbook_bytes(csv_path: Path) -> tuple[bytes, int]:
    """Convert a catalogue CSV into the workbook the dashboard import expects."""
    from openpyxl import Workbook  # noqa: PLC0415

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{csv_path} contains no catalogue rows.")

    unknown = set(rows[0]) - set(catalogue_import.HEADERS)
    if unknown:
        raise SystemExit(f"{csv_path} has unsupported columns: {sorted(unknown)}")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"
    sheet.append(catalogue_import.HEADERS)
    for row in rows:
        values: list[object] = []
        for header in catalogue_import.HEADERS:
            raw = (row.get(header) or "").strip()
            if not raw:
                values.append(None)
            elif header == "Price":
                values.append(float(Decimal(raw)))
            elif header in ("Stock Quantity", "Reorder Level"):
                values.append(int(raw))
            else:
                values.append(raw)
        sheet.append(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), len(rows)


def command_catalogue(args: argparse.Namespace, settings: Settings) -> int:
    admin_id = _admin_id(args)
    tenant = _tenant(settings, admin_id, args.tenant_slug)
    tenant_id = str(tenant["id"])
    csv_path = _resolve(args.csv)
    content, row_count = _workbook_bytes(csv_path)

    print(f"tenant   : {tenant['name']} ({tenant['slug']}) {tenant_id}")
    print(f"workbook : {csv_path} ({row_count} rows, {len(content)} bytes)")
    print(
        f"before   : {tenant['product_count']} products, "
        f"{tenant['active_variant_count']} active variants"
    )

    preview = catalogue_import.preview_import(
        settings, admin_id, tenant_id, csv_path.with_suffix(".xlsx").name, content
    )
    print(f"preview  : {preview['counts']}")
    if preview["errors"]:
        for error in preview["errors"][:20]:
            print(f"  row {error['row_number']}: {error['message']}")
        print(f"REFUSED  : {len(preview['errors'])} validation error(s); nothing was written.")
        return 1
    if args.dry_run:
        print("DRY RUN  : preview recorded; the catalogue was not applied.")
        return 0

    applied = catalogue_import.apply_import(settings, admin_id, tenant_id, preview["batch_id"])
    print(f"applied  : {applied}")
    return 0


def command_settings(args: argparse.Namespace, settings: Settings) -> int:
    admin_id = _admin_id(args)
    tenant = _tenant(settings, admin_id, args.tenant_slug)
    tenant_id = str(tenant["id"])
    document = json.loads(_resolve(args.json).read_text(encoding="utf-8"))

    profile = document.get("profile") or {}
    if profile:
        tenants.update_profile(settings, admin_id, tenant_id, profile)
        print(f"profile  : {profile}")
    documents = {
        key: document[key] for key, _label in tenants.RULE_DOCUMENTS if key in document
    }
    if not documents:
        print("REFUSED  : no business_rules / bot_persona / business_hours / configuration.")
        return 1
    tenants.update_rules(settings, admin_id, tenant_id, documents)
    print(f"rules    : replaced {sorted(documents)}")
    return 0


def command_verify(args: argparse.Namespace, settings: Settings) -> int:
    admin_id = _admin_id(args)
    tenant = _tenant(settings, admin_id, args.tenant_slug)
    tenant_id = str(tenant["id"])
    print(f"tenant    : {tenant['name']} ({tenant['slug']}) {tenant_id}")
    print(f"status    : {tenant['status']}")
    print(f"channel   : {tenant['channel_status']}")
    print(
        f"catalogue : {tenant['product_count']} products, "
        f"{tenant['active_variant_count']} active variants, "
        f"{tenant['owner_count']} owners"
    )

    with tenant_admin(tenant_id, settings.app_database_url) as admin:
        catalogue = admin.list_catalogue()
        print(f"read back : {len(catalogue)} products via the application role")
        for product in catalogue[:2]:
            for variant in product["variants"]:
                print(
                    f"   {product['name']:<13} {variant['name']:<5} "
                    f"{variant['price']} {variant['availability']} sku={variant['sku']}"
                )
        if args.lookup:
            name, _, weight = args.lookup.partition(":")
            resolved = admin.lookup_price(name, int(weight))
            if resolved is None:
                print(f"lookup    : FAILED for {name!r} at {weight}kg")
                return 1
            print(
                f"lookup    : {name!r} at {weight}kg -> "
                f"{resolved['variant']['price']} (sku {resolved['variant']['sku']})"
            )

    readiness = tenants.readiness(settings, admin_id, tenant_id)
    print("readiness :")
    for check in readiness.get("checks", []):
        flag = "ok" if check.get("ok") else "NO"
        required = "required" if check.get("required") else "optional"
        print(f"   [{flag}] {check.get('key'):<26} {required:<8} {check.get('detail', '')}")
    print(f"ready     : {readiness.get('ready')}")
    return 0 if readiness.get("ready") else 1


def command_status(args: argparse.Namespace, settings: Settings) -> int:
    admin_id = _admin_id(args)
    tenant = _tenant(settings, admin_id, args.tenant_slug)
    status = tenants.change_status(
        settings, admin_id, str(tenant["id"]), args.set, waive_owner=args.waive_owner
    )
    print(f"status    : {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env", help="load missing variables from here")
    parser.add_argument("--admin-user-id", default=None, help="verified platform administrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalogue = subparsers.add_parser("catalogue", help="validate and load a catalogue CSV")
    catalogue.add_argument("--tenant-slug", required=True)
    catalogue.add_argument("--csv", required=True)
    catalogue.add_argument("--dry-run", action="store_true")
    catalogue.set_defaults(func=command_catalogue)

    settings_cmd = subparsers.add_parser("settings", help="apply a settings JSON document")
    settings_cmd.add_argument("--tenant-slug", required=True)
    settings_cmd.add_argument("--json", required=True)
    settings_cmd.set_defaults(func=command_settings)

    verify = subparsers.add_parser("verify", help="report catalogue, readiness and lookups")
    verify.add_argument("--tenant-slug", required=True)
    verify.add_argument("--lookup", default=None, help="sample price check, e.g. 'Vanilla:2'")
    verify.set_defaults(func=command_verify)

    status = subparsers.add_parser("status", help="activate or park a tenant")
    status.add_argument("--tenant-slug", required=True)
    status.add_argument("--set", required=True, choices=("active", "suspended", "inactive"))
    status.add_argument("--waive-owner", action="store_true")
    status.set_defaults(func=command_status)

    args = parser.parse_args(argv)
    settings = _settings(_resolve(args.env_file))
    try:
        return args.func(args, settings)
    except ServiceError as error:
        print(f"ERROR    : {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

