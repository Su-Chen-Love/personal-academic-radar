import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.product import (
    asset_text,
    classify_low_priority,
    import_fulltext,
    initialize_installation,
    migrate_legacy_model_config,
    source_candidates,
    source_coverage,
)
from academic_radar.storage import connect, upgrade_database


class ProductTests(unittest.TestCase):
    def test_packaged_installation_assets_match_repository_examples(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("config.example.toml", "research-profile.example.md"):
            repository = (root / "assets" / name).read_text(encoding="utf-8")
            packaged = (root / "src" / "academic_radar" / "assets" / name).read_text(encoding="utf-8")
            self.assertEqual(repository, packaged)
            self.assertEqual(asset_text(name, "fallback"), packaged)

    def test_init_creates_private_state_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            first = initialize_installation(state)
            second = initialize_installation(state)
            self.assertTrue(Path(first["config"]).exists())
            self.assertTrue(Path(first["profile"]).exists())
            self.assertEqual(first["active_profile_id"], second["active_profile_id"])
            self.assertTrue(second["preserved"])

    def test_legacy_model_config_is_backed_up_and_removed(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"config.toml"
            path.write_text('state_dir = "."\n[llm]\nprovider = "legacy"\nmodel = "old"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234-5678"\n',encoding="utf-8")
            backup=migrate_legacy_model_config(path)
            self.assertTrue(Path(backup).exists())
            self.assertNotIn("[llm]",path.read_text())
            self.assertIn("[[sources]]",path.read_text())

    def test_init_backs_up_database_before_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            first = initialize_installation(state)
            db = connect(Path(first["database"]))
            with db:
                db.execute("DELETE FROM schema_migrations WHERE version=6")
            db.close()

            repaired = initialize_installation(state)

            self.assertIsNotNone(repaired["pre_upgrade_backup"])
            self.assertTrue(Path(repaired["pre_upgrade_backup"]).exists())

    def test_init_backfills_legacy_abstract_source_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            first = initialize_installation(state)
            db = connect(Path(first["database"]))
            with db:
                db.execute(
                    """INSERT INTO papers(
                    identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
                    abstract_source,low_priority
                    ) VALUES('doi:10.1/editorial','10.1/editorial','Editorial Board','Existing abstract',
                    'Journal','','','[]','now','now','unknown',0)"""
                )
                db.execute(
                    """INSERT INTO screenings(
                    identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,screened_at
                    ) VALUES('doi:10.1/editorial','profile','codex-agent','test',0,0.1,'No','[]',0.95,'now')"""
                )
                db.execute(
                    """INSERT INTO papers(
                    identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
                    abstract_source,low_priority
                    ) VALUES('doi:10.1/missing','10.1/missing','Regular Paper','','Journal','','','[]',
                    'now','now','unknown',0)"""
                )
                db.execute(
                    """INSERT INTO screenings(
                    identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,screened_at
                    ) VALUES('doi:10.1/missing','profile','codex-agent','test',1,0.8,'Title','[]',0.95,'now')"""
                )
            db.close()

            repaired = initialize_installation(state)

            self.assertEqual(
                repaired["metadata_repaired"],
                {"abstract_sources": 2, "low_priority": 1, "confidence_capped": 1},
            )
            db = connect(Path(first["database"]))
            row = db.execute(
                "SELECT abstract_source,low_priority,low_priority_reason FROM papers WHERE identity='doi:10.1/editorial'"
            ).fetchone()
            self.assertEqual(row["abstract_source"], "existing")
            self.assertEqual(row["low_priority"], 1)
            self.assertIn("editorial", row["low_priority_reason"])
            confidence = db.execute(
                "SELECT confidence FROM screenings WHERE identity='doi:10.1/missing'"
            ).fetchone()[0]
            self.assertEqual(confidence, 0.5)
            db.close()

    def test_low_priority_classifier_marks_editorial_items(self):
        low, reason = classify_low_priority("Editorial Board", "Journal")
        self.assertTrue(low)
        self.assertIn("editorial", reason)

    def test_import_fulltext_deduplicates_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO papers(
                    identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("doi:10.1/x", "10.1/x", "A Good Paper", "Abstract", "Venue",
                     "2026-01-01", "u", "[]", "now", "now"),
                )
            db.close()
            first = import_fulltext(db_path, root, "doi:10.1/x", "original.pdf", b"%PDF-1.4 content")
            second = import_fulltext(db_path, root, "doi:10.1/x", "copy.pdf", b"%PDF-1.4 content")
            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertTrue(Path(first["stored_path"]).exists())

    def test_import_fulltext_keeps_a_readable_name_for_each_paper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                for suffix in ("a", "b"):
                    db.execute(
                        """INSERT INTO papers(
                        identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (f"doi:10.1/{suffix}", f"10.1/{suffix}", f"Paper {suffix}", "", "V",
                         "2026-01-01", "u", "[]", "now", "now"),
                    )
            db.close()

            first = import_fulltext(db_path, root, "doi:10.1/a", "a.pdf", b"%PDF shared")
            second = import_fulltext(db_path, root, "doi:10.1/b", "b.pdf", b"%PDF shared")

            self.assertFalse(second["deduplicated"])
            self.assertNotEqual(first["stored_path"], second["stored_path"])
            self.assertTrue(Path(second["stored_path"]).name.startswith("作者未知_2026_Paper b"))
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fulltext_files").fetchone()[0], 2)
            db.close()

    def test_import_fulltext_updates_the_current_file_and_keeps_one_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO papers(identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("doi:10.1/update", "10.1/update", "A / Precise: Paper", "", "V", "2026-01-01", "u",
                     '["Doe, Jane", "Smith, John"]', "now", "now"),
                )
            db.close()
            first = import_fulltext(db_path, root, "doi:10.1/update", "first.pdf", b"%PDF first")
            updated = import_fulltext(db_path, root, "doi:10.1/update", "second.pdf", b"%PDF second")
            self.assertTrue(updated["updated"])
            self.assertIn("Doe, Jane、Smith, John_2026_A Precise Paper.pdf", Path(updated["stored_path"]).name)
            self.assertEqual(Path(updated["stored_path"]).read_bytes(), b"%PDF second")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fulltext_files WHERE identity='doi:10.1/update'").fetchone()[0], 1)
            db.close()

    def test_source_candidates_merges_crossref_and_openalex(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.payload

        crossref = b'{"message":{"items":[{"title":"Journal A","ISSN":["1234-5678"],"publisher":"P"}]}}'
        openalex = b'{"results":[{"display_name":"Journal A","issn":["1234-5678"],"id":"https://openalex.org/S1","type":"journal"}]}'
        with patch("academic_radar.product.urllib.request.urlopen", side_effect=[Response(crossref), Response(openalex)]):
            candidates = source_candidates("Journal A")
        self.assertEqual(candidates[0]["issn"], "1234-5678")
        self.assertEqual(candidates[0]["openalex_id"], "S1")

    def test_source_coverage_deduplicates_provider_observations(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                db.execute(
                    """INSERT INTO papers(
                    identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                    ) VALUES('doi:10.1/x','10.1/x','Paper','Abstract','Journal','2026-01-01','','[]','now','now')"""
                )
                db.execute("INSERT INTO observations VALUES('doi:10.1/x','Journal','now')")
                db.execute("INSERT INTO observations VALUES('doi:10.1/x','Journal / OpenAlex','now')")
                db.execute("INSERT INTO observations VALUES('doi:10.1/x','Journal / Official volume 12','now')")
            db.close()

            coverage = source_coverage(db_path, [{"name": "Journal"}])["Journal"]

            self.assertEqual(coverage["paper_count"], 1)
            self.assertEqual(coverage["abstract_count"], 1)
            self.assertEqual(coverage["abstract_percent"], 100.0)

    def test_source_coverage_counts_official_issue_observations(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            upgrade_database(db_path)
            db = connect(db_path)
            with db:
                for suffix in ("x", "y"):
                    db.execute(
                        """INSERT INTO papers(
                        identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (f"doi:10.1/{suffix}", f"10.1/{suffix}", suffix, "Abstract", "Journal",
                         "2026-01-01", "", "[]", "now", "now"),
                    )
                db.execute("INSERT INTO observations VALUES('doi:10.1/x','Journal / Official volume 12','now')")
                db.execute("INSERT INTO observations VALUES('doi:10.1/y','Journal / OpenAlex','now')")
            db.close()

            coverage = source_coverage(db_path, [{"name": "Journal"}])["Journal"]

            self.assertEqual(coverage["paper_count"], 2)
            self.assertEqual(coverage["abstract_count"], 2)

if __name__ == "__main__":
    unittest.main()
