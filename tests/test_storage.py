import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.storage import (
    LEGACY_MIGRATION_CHECKSUMS,
    backup_database,
    database_status,
    latest_schema_version,
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
            self.assertEqual(first["applied"], list(range(1, latest_schema_version() + 1)))
            self.assertEqual(second["applied"], [])
            status = database_status(db_path)
            self.assertEqual(status["schema_version"], latest_schema_version())
            self.assertEqual(status["integrity"], "ok")
            self.assertIn("paper_feedback", status["counts"])
            self.assertIn("fulltext_files", status["counts"])

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
            self.assertEqual(result["applied"], list(range(1, latest_schema_version() + 1)))
            self.assertEqual(database_status(db_path)["counts"]["papers"], 1)

    def test_partially_upgraded_legacy_database_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "partial.sqlite3"
            db = sqlite3.connect(db_path)
            db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE papers(identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL,
              abstract TEXT, venue TEXT, published TEXT, url TEXT, authors_json TEXT,
              first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
              abstract_source TEXT NOT NULL DEFAULT 'unknown');
            CREATE TABLE screenings(identity TEXT NOT NULL, profile_hash TEXT NOT NULL,
              provider TEXT NOT NULL, model TEXT, relevant INTEGER NOT NULL, score REAL NOT NULL,
              reasons TEXT, themes_json TEXT, confidence REAL, screened_at TEXT NOT NULL,
              PRIMARY KEY(identity, profile_hash, provider, model));
            CREATE TABLE agent_jobs(run_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL,
              status TEXT NOT NULL, queue_path TEXT, results_path TEXT,
              exported_count INTEGER NOT NULL DEFAULT 0,
              imported_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              imported_at TEXT, profile_version_id INTEGER);
            """)
            db.commit()
            db.close()

            result = upgrade_database(db_path)

            self.assertEqual(result["applied"], list(range(1, latest_schema_version() + 1)))
            db = sqlite3.connect(db_path)
            paper_columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_jobs)")}
            self.assertIn("low_priority", paper_columns)
            self.assertIn("feedback_snapshot_json", job_columns)
            db.close()

    def test_future_schema_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "future.sqlite3"
            upgrade_database(db_path)
            db = sqlite3.connect(db_path)
            db.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(999,'future','x','now')"
            )
            before = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            db.commit()
            db.close()

            with self.assertRaisesRegex(RuntimeError, "unknown future migrations"):
                upgrade_database(db_path)

            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], before)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()

    def test_known_legacy_migration_checksum_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "legacy-checksum.sqlite3"
            upgrade_database(db_path)
            db = sqlite3.connect(db_path)
            db.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=10",
                (next(iter(LEGACY_MIGRATION_CHECKSUMS[10])),),
            )
            db.commit()
            db.close()

            result = upgrade_database(db_path)

            self.assertEqual(result["applied"], [])

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

    def test_state_migration_rebases_absolute_config_and_fulltext_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "legacy-state"
            destination = root / "new-state"
            (source / "fulltexts").mkdir(parents=True)
            (source / "config.toml").write_text(
                f'state_dir = {json.dumps(str(source.resolve()))}\nprofile_file = "research-profile.md"\n',
                encoding="utf-8",
            )
            (source / "research-profile.md").write_text("profile", encoding="utf-8")
            pdf = source / "fulltexts" / "paper.pdf"
            pdf.write_bytes(b"%PDF test")
            upgrade_database(source / "papers.sqlite3")
            db = sqlite3.connect(source / "papers.sqlite3")
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES('doi:10.1/x','10.1/x','Paper','','V','','','[]','now','now')"""
            )
            db.execute(
                """INSERT INTO fulltext_files(
                identity,stored_path,original_name,sha256,size_bytes,imported_at
                ) VALUES('doi:10.1/x',?,'paper.pdf','hash',9,'now')""",
                (str(pdf.resolve()),),
            )
            db.commit()
            db.close()

            result = migrate_state(source, destination)

            self.assertTrue(result["config_rebased"])
            self.assertEqual(result["fulltext_paths_rebased"], 1)
            self.assertIn('state_dir = "."', (destination / "config.toml").read_text())
            db = sqlite3.connect(destination / "papers.sqlite3")
            stored_path = Path(db.execute("SELECT stored_path FROM fulltext_files").fetchone()[0])
            db.close()
            self.assertEqual(stored_path, (destination / "fulltexts" / "paper.pdf").resolve())
            self.assertTrue(stored_path.exists())


if __name__ == "__main__":
    unittest.main()
