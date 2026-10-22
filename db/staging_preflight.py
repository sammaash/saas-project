"""Read-only staging preflight: prove this environment cannot reach production.

Parses configuration only. It opens no database connection, reads no credential beyond
comparing the *shape* of the database URLs, and never prints a host, password or token --
only role names, project references and variable names. Safe to run anywhere, including
inside the staging service.

Exit codes:
    0  no violations (safe to treat this environment as staging)
    1  at least one violation (do not deploy or migrate from here)

Usage:
    python db/staging_preflight.py                       # check the process environment
    python db/staging_preflight.py --env-file .env.staging
    python db/staging_preflight.py --json                # machine-readable

The same checks run automatically at boot when SAAS_ENVIRONMENT=staging
(platform_admin/config.py::_staging_guard); this script exists to catch a mistake before a
deployment, with an explanation of every finding.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # allow `python db/staging_preflight.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from platform_admin.config import FORBIDDEN_DB_USERS, project_refs  # noqa: E402

FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

#: Which role each application URL must use. Anything else is either a privilege escalation
#: or a connection that bypasses row level security.
EXPECTED_ROLES = {
    "SAAS_DATABASE_URL": "app_backend",
    "SAAS_PLATFORM_DATABASE_URL": "saas_platform_admin",
}

WHATSAPP_TOKEN_PREFIX = "WHATSAPP_ACCESS_TOKEN_"


@dataclass(frozen=True)
class Finding:
    """One preflight result. ``level`` is FAIL, WARN or INFO."""

    level: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}

    def __str__(self) -> str:
        return f"[{self.level}] {self.code}: {self.message}"


def _role(dsn: str) -> str:
    """The PostgreSQL login a DSN uses, without the pooler's project suffix."""
    try:
        username = urlsplit(dsn).username or ""
    except ValueError:
        return ""
    return username.split(".")[0].lower()


def analyse(environ: dict[str, str]) -> list[Finding]:
    """Every staging-isolation check, as data. Pure: no I/O, no connections."""
    findings: list[Finding] = []

    def add(level: str, code: str, message: str) -> None:
        findings.append(Finding(level, code, message))

    environment = (environ.get("SAAS_ENVIRONMENT") or "").strip().lower()
    if environment != "staging":
        add(
            FAIL,
            "environment",
            f"SAAS_ENVIRONMENT is {environment or 'unset'}; this environment cannot be "
            "certified as staging.",
        )
    else:
        add(INFO, "environment", "SAAS_ENVIRONMENT=staging")

    production_ref = (environ.get("SAAS_PRODUCTION_PROJECT_REF") or "").strip().lower()
    if not production_ref:
        add(
            FAIL,
            "production-ref",
            "SAAS_PRODUCTION_PROJECT_REF is not set, so the production project cannot be "
            "excluded.",
        )
    else:
        add(INFO, "production-ref", f"production project to avoid: {production_ref}")

    referenced: list[str] = []
    for name, expected in EXPECTED_ROLES.items():
        dsn = (environ.get(name) or "").strip()
        if not dsn:
            add(FAIL, "dsn", f"{name} is not set.")
            continue

        role = _role(dsn)
        if role in FORBIDDEN_DB_USERS:
            add(FAIL, "privileged-role", f"{name} uses '{role}', which bypasses the checks.")
        elif role != expected:
            add(FAIL, "role", f"{name} uses '{role or 'unknown'}'; expected '{expected}'.")
        else:
            add(INFO, "role", f"{name} uses {role}")

        refs = project_refs(dsn)
        referenced.extend(sorted(refs))
        if not refs:
            add(
                WARN,
                "project",
                f"{name} names no Supabase project reference (local or non-Supabase host?).",
            )
        elif production_ref and production_ref in refs:
            add(
                FAIL,
                "production-dsn",
                f"{name} resolves to the production project {production_ref}.",
            )
        else:
            add(INFO, "project", f"{name} project: {', '.join(sorted(refs))}")

    if production_ref and production_ref in project_refs(environ.get("SUPABASE_URL")):
        add(FAIL, "production-supabase", "SUPABASE_URL resolves to the production project.")
    if len(set(referenced)) > 1:
        add(
            FAIL,
            "split-project",
            "The application URLs resolve to different projects: "
            + ", ".join(sorted(set(referenced))),
        )


    sending = (environ.get("WHATSAPP_SEND_ENABLED") or "").strip()
    if not sending:
        add(
            FAIL,
            "whatsapp-send",
            "WHATSAPP_SEND_ENABLED is not set; declare it explicitly (0 unless a dedicated "
            "staging test number is configured).",
        )
    elif sending not in {"0", "1"}:
        add(FAIL, "whatsapp-send", f"WHATSAPP_SEND_ENABLED={sending!r} is not 0 or 1.")
    elif sending == "0":
        add(INFO, "whatsapp-send", "WhatsApp sending is disabled (safe default).")
    else:
        tokens = sorted(key for key in environ if key.startswith(WHATSAPP_TOKEN_PREFIX))
        add(INFO, "whatsapp-send", "WhatsApp sending is enabled.")
        if tokens:
            add(INFO, "whatsapp-token", "tenant tokens configured: " + ", ".join(tokens))
            add(
                WARN,
                "whatsapp-token-origin",
                "confirm every token above belongs to a staging/test number, never production.",
            )
        else:
            add(
                WARN,
                "whatsapp-token",
                "sending is enabled but no WHATSAPP_ACCESS_TOKEN_* is configured; every send "
                "will fail.",
            )

    if (environ.get("SAAS_OPERATOR_DATABASE_URL") or "").strip():
        add(
            FAIL,
            "operator-dsn",
            "SAAS_OPERATOR_DATABASE_URL is present; owner credentials must not be on a web "
            "service.",
        )
    if (environ.get("SAAS_TEST_DATABASE_URL") or "").strip():
        add(
            WARN,
            "test-dsn",
            "SAAS_TEST_DATABASE_URL is for local/CI only, never a deployed service.",
        )
    if (environ.get("PLATFORM_DEV_MODE") or "0").strip() == "1":
        add(FAIL, "dev-mode", "PLATFORM_DEV_MODE must be 0 in staging.")
    if (environ.get("PLATFORM_INSECURE_COOKIES") or "0").strip() == "1":
        add(FAIL, "dev-mode", "PLATFORM_INSECURE_COOKIES must be 0 in staging.")
    if (environ.get("PLATFORM_ALLOW_ACTIVE_CHANNELS") or "0").strip() == "1":
        add(
            WARN,
            "channels",
            "PLATFORM_ALLOW_ACTIVE_CHANNELS=1 lets operators activate live channels; leave 0 "
            "unless testing inbound webhooks.",
        )
    return findings


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE loader; the process environment always wins over the file."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--env-file",
        help="merge KEY=VALUE pairs from this file; values already set in the environment win",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.env_file:
        path = Path(args.env_file)
        if not path.is_file():
            parser.error(f"no such env file: {path}")
        for key, value in _load_env_file(path).items():
            environ.setdefault(key, value)

    findings = analyse(environ)
    failures = [finding for finding in findings if finding.level == FAIL]
    warnings = [finding for finding in findings if finding.level == WARN]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "environment": (environ.get("SAAS_ENVIRONMENT") or "").strip().lower(),
                    "findings": [finding.as_dict() for finding in findings],
                },
                indent=2,
            )
        )
    else:
        for finding in findings:
            print(finding)
        print()
        print(f"{len(failures)} failure(s), {len(warnings)} warning(s)")
        print("STAGING PREFLIGHT: " + ("PASS" if not failures else "REFUSED"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

