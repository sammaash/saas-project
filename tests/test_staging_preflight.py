"""Unit tests for the read-only staging preflight (``db/staging_preflight.py``).

Pure: no database, no real environment mutation (only a patched copy), and they assert that
the report never prints a password or a host -- only roles, project references and variable
names.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from db import staging_preflight as preflight

PRODUCTION_REF = "prodref0000000000000"
STAGING_REF = "stagref0000000000000"
SECRET_PASSWORD = "super-secret-staging-password"


def _pooled(role: str, ref: str, password: str = "pw") -> str:
    return (
        f"postgresql://{role}.{ref}:{password}@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    )


SAFE = {
    "SAAS_ENVIRONMENT": "staging",
    "SAAS_PRODUCTION_PROJECT_REF": PRODUCTION_REF,
    "SAAS_DATABASE_URL": _pooled("app_backend", STAGING_REF, SECRET_PASSWORD),
    "SAAS_PLATFORM_DATABASE_URL": _pooled(
        "saas_platform_admin", STAGING_REF, SECRET_PASSWORD
    ),
    "SUPABASE_URL": f"https://{STAGING_REF}.supabase.co",
    "WHATSAPP_SEND_ENABLED": "0",
}


class StagingPreflightTests(unittest.TestCase):
    def findings(self, **changes):
        environ = dict(SAFE)
        environ.update(changes)
        return preflight.analyse({key: value for key, value in environ.items() if value is not None})

    @staticmethod
    def codes(findings, level):
        return [finding.code for finding in findings if finding.level == level]

    def test_a_clean_staging_environment_passes(self):
        self.assertEqual(self.codes(preflight.analyse(SAFE), preflight.FAIL), [])

    def test_an_undeclared_environment_is_refused(self):
        findings = self.findings(SAAS_ENVIRONMENT=None)
        self.assertIn("environment", self.codes(findings, preflight.FAIL))

    def test_a_production_application_database_is_refused(self):
        findings = self.findings(SAAS_DATABASE_URL=_pooled("app_backend", PRODUCTION_REF))
        self.assertIn("production-dsn", self.codes(findings, preflight.FAIL))

    def test_a_production_platform_database_is_refused(self):
        findings = self.findings(
            SAAS_PLATFORM_DATABASE_URL=_pooled("saas_platform_admin", PRODUCTION_REF)
        )
        self.assertIn("production-dsn", self.codes(findings, preflight.FAIL))

    def test_a_production_supabase_url_is_refused(self):
        findings = self.findings(SUPABASE_URL=f"https://{PRODUCTION_REF}.supabase.co")
        self.assertIn("production-supabase", self.codes(findings, preflight.FAIL))

    def test_the_production_reference_must_be_named(self):
        findings = self.findings(SAAS_PRODUCTION_PROJECT_REF=None)
        self.assertIn("production-ref", self.codes(findings, preflight.FAIL))

    def test_the_wrong_role_is_refused(self):
        findings = self.findings(SAAS_DATABASE_URL=_pooled("saas_platform_admin", STAGING_REF))
        self.assertIn("role", self.codes(findings, preflight.FAIL))

    def test_a_privileged_role_is_refused(self):
        findings = self.findings(SAAS_DATABASE_URL=_pooled("postgres", STAGING_REF))
        self.assertIn("privileged-role", self.codes(findings, preflight.FAIL))

    def test_owner_credentials_are_refused(self):
        findings = self.findings(
            SAAS_OPERATOR_DATABASE_URL=_pooled("postgres", STAGING_REF)
        )
        self.assertIn("operator-dsn", self.codes(findings, preflight.FAIL))

    def test_an_implicit_sending_setting_is_refused(self):
        findings = self.findings(WHATSAPP_SEND_ENABLED=None)
        self.assertIn("whatsapp-send", self.codes(findings, preflight.FAIL))

    def test_sending_without_a_token_warns_but_passes(self):
        findings = self.findings(WHATSAPP_SEND_ENABLED="1")
        self.assertIn("whatsapp-token", self.codes(findings, preflight.WARN))
        self.assertNotIn("whatsapp-send", self.codes(findings, preflight.FAIL))

    def test_two_different_projects_are_refused(self):
        findings = self.findings(
            SAAS_PLATFORM_DATABASE_URL=_pooled(
                "saas_platform_admin", "otherref0000000000000"
            )
        )
        self.assertIn("split-project", self.codes(findings, preflight.FAIL))

    def test_dev_mode_is_refused(self):
        findings = self.findings(PLATFORM_DEV_MODE="1")
        self.assertIn("dev-mode", self.codes(findings, preflight.FAIL))

    def test_a_local_database_is_warned_about_not_failed(self):
        findings = self.findings(
            SAAS_DATABASE_URL="postgresql://app_backend:pw@localhost:5432/postgres",
            SAAS_PLATFORM_DATABASE_URL="postgresql://saas_platform_admin:pw@localhost:5432/postgres",
        )
        self.assertIn("project", self.codes(findings, preflight.WARN))
        self.assertEqual(self.codes(findings, preflight.FAIL), [])

    def test_exit_code_is_zero_only_when_nothing_fails(self):
        for environ, expected in ((SAFE, 0), ({**SAFE, "WHATSAPP_SEND_ENABLED": ""}, 1)):
            with self.subTest(expected=expected), patch.dict(os.environ, environ, clear=True):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(preflight.main([]), expected)

    def test_the_report_never_prints_a_password_or_host(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, SAFE, clear=True), redirect_stdout(buffer):
            preflight.main([])
        output = buffer.getvalue()
        self.assertNotIn(SECRET_PASSWORD, output)
        self.assertNotIn("pooler.supabase.com", output)
        self.assertIn("STAGING PREFLIGHT: PASS", output)

    def test_json_output_marks_success_without_leaking_secrets(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, SAFE, clear=True), redirect_stdout(buffer):
            self.assertEqual(preflight.main(["--json"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "staging")
        self.assertNotIn(SECRET_PASSWORD, buffer.getvalue())

    def test_an_env_file_is_merged_and_the_process_environment_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env.staging"
            path.write_text(
                "SAAS_ENVIRONMENT=staging\n"
                f"SAAS_PRODUCTION_PROJECT_REF={PRODUCTION_REF}\n"
                f"SAAS_DATABASE_URL={_pooled('app_backend', PRODUCTION_REF)}\n"
                f"SAAS_PLATFORM_DATABASE_URL={_pooled('saas_platform_admin', STAGING_REF)}\n"
                f"SUPABASE_URL=https://{STAGING_REF}.supabase.co\n"
                "WHATSAPP_SEND_ENABLED=0\n",
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with patch.dict(
                os.environ,
                {"SAAS_DATABASE_URL": _pooled("app_backend", STAGING_REF)},
                clear=True,
            ):
                with redirect_stdout(buffer):
                    code = preflight.main(["--env-file", str(path)])

        self.assertEqual(code, 0, "the environment value must override the file")
        self.assertNotIn(SECRET_PASSWORD, buffer.getvalue())

    def test_an_env_file_pointing_at_production_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env.staging"
            path.write_text(
                "SAAS_ENVIRONMENT=staging\n"
                f"SAAS_PRODUCTION_PROJECT_REF={PRODUCTION_REF}\n"
                f"SAAS_DATABASE_URL={_pooled('app_backend', PRODUCTION_REF)}\n"
                f"SAAS_PLATFORM_DATABASE_URL={_pooled('saas_platform_admin', STAGING_REF)}\n"
                "WHATSAPP_SEND_ENABLED=0\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                code = preflight.main(["--env-file", str(path)])

        self.assertEqual(code, 1)

    def test_a_missing_env_file_is_a_usage_error(self):
        with patch.dict(os.environ, SAFE, clear=True), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    preflight.main(["--env-file", "does-not-exist.env"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

