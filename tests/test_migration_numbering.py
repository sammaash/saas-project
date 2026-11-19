"""Guard: the migration history stays unambiguous (roadmap §9.0 step 4).

Two unapplied drafts elsewhere once reused applied numbers (a second 0042 and a second
0037). Applying either would have been silently skipped by name or would have forked the
history. These tests make any such file fail CI before it can reach a database.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter

from db.run_migrations import MIGRATIONS_DIR, _checksum, _sql_files

try:
    from .saas_test_support import SaasDatabaseTestCase
except ImportError:
    from saas_test_support import SaasDatabaseTestCase  # type: ignore[no-redef]

NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
SIBLING_DIRS = (MIGRATIONS_DIR.parent / "local", MIGRATIONS_DIR.parent / "seeds")


def _prefixes(directory) -> list[str]:
    return [path.name[:4] for path in _sql_files(directory)]


class MigrationNumberingTests(unittest.TestCase):
    def test_every_migration_name_has_four_digit_prefix_and_snake_case(self) -> None:
        bad = [p.name for p in _sql_files(MIGRATIONS_DIR) if not NAME.match(p.name)]
        self.assertEqual(bad, [], "migration filenames must look like 0045_some_change.sql")

    def test_migration_numbers_are_unique(self) -> None:
        duplicates = sorted(n for n, c in Counter(_prefixes(MIGRATIONS_DIR)).items() if c > 1)
        self.assertEqual(duplicates, [], f"duplicate migration numbers: {duplicates}")

    def test_migration_numbers_are_contiguous_from_0001(self) -> None:
        numbers = [int(n) for n in _prefixes(MIGRATIONS_DIR)]
        self.assertTrue(numbers, "no migrations found")
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)),
                         "migration numbers must run 0001..N with no gap or reuse")

    def test_local_and_seed_scripts_have_unique_numbers(self) -> None:
        for directory in SIBLING_DIRS:
            with self.subTest(directory=directory.name):
                duplicates = sorted(n for n, c in Counter(_prefixes(directory)).items() if c > 1)
                self.assertEqual(duplicates, [])


class MigrationLedgerParityTests(SaasDatabaseTestCase):
    """On a migrated database, ledger rows and files must match one-to-one by checksum."""

    def test_ledger_matches_files_exactly(self) -> None:
        files = {p.name: _checksum(p.read_text(encoding="utf-8")) for p in _sql_files(MIGRATIONS_DIR)}
        with self.superuser() as cursor:
            cursor.execute("select version, checksum from public.saas_schema_migrations")
            ledger = dict(cursor.fetchall())
        self.assertEqual(sorted(set(ledger) - set(files)), [], "ledger rows with no file")
        self.assertEqual(sorted(set(files) - set(ledger)), [], "files never applied")
        mismatched = sorted(n for n in files if ledger.get(n) != files[n])
        self.assertEqual(mismatched, [], "checksum mismatches between ledger and files")


if __name__ == "__main__":
    unittest.main()
