import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.storage import (
    backup_database,
    database_status,
    migrate_state,
    restore_database,
    upgrade_database,
)


class StorageTests(unittest.TestCase):
    def test_upgrade_is_idempotent_and_creates_product_tables(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            first = upgrade_database(db_path)
            second = upgrade_database(db_path)
            self.assertEqual(first["applied"], [1, 2, 3])
            self.assertEqual(second["applied"], [])
            status = database_status(db_path)
            self.assertEqual(status["schema_version"], 3)
            self.assertEqual(status["integrity"], "ok")
            self.assertIn("paper_feedback", status["counts"])

    def test_legacy_database_is_upgraded_without_losing_papers(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy.sqlite3"
            db = sqlite3.connect(db_path)
            db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE papers(identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL,
              abstract TEXT, venue TEXT, published TEXT, url TEXT, authors_json TEXT,
              first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO papers VALUES('doi:10.1/example','10.1/example','Example','','Venue',
              '2026-01-01','https://doi.org/10.1/example','[]','now','now');
            """)
            db.commit()
            db.close()
            result = upgrade_database(db_path)
            self.assertEqual(result["applied"], [1, 2, 3])
            self.assertEqual(database_status(db_path)["counts"]["papers"], 1)

    def test_backup_restore_and_pre_restore_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.sqlite3"
            upgrade_database(source)
            db = sqlite3.connect(source)
            db.execute("INSERT INTO meta VALUES('marker','source')")
            db.commit()
            db.close()
            backup = root / "backup.sqlite3"
            self.assertEqual(backup_database(source, backup)["integrity"], "ok")

            destination = root / "destination.sqlite3"
            upgrade_database(destination)
            result = restore_database(backup, destination, replace=True)
            self.assertTrue(Path(result["preserved_previous"]).exists())
            db = sqlite3.connect(destination)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0], "source")
            db.close()

    def test_state_migration_copies_artifacts_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "legacy-state"
            destination = root / "new-state"
            source.mkdir()
            (source / "config.toml").write_text('state_dir = "."', encoding="utf-8")
            (source / "research-profile.md").write_text("profile", encoding="utf-8")
            upgrade_database(source / "papers.sqlite3")
            result = migrate_state(source, destination)
            self.assertEqual(result["database"]["integrity"], "ok")
            self.assertTrue(result["profile_seeded"])
            self.assertTrue((destination / "config.toml").exists())
            self.assertEqual(database_status(destination / "papers.sqlite3")["counts"]["profile_versions"], 1)
            manifest = json.loads((destination / "migration-manifest.json").read_text())
            self.assertEqual(manifest["destination"], str(destination.resolve()))


if __name__ == "__main__":
    unittest.main()
