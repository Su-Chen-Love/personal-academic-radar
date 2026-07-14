import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar.enrichment import (
    MetadataClient,
    apply_manual_import,
    enrich_abstracts,
    export_missing_task_package,
    lookup_semantic_scholar,
    lookup_publisher,
    prime_semantic_scholar_batch,
    preview_manual_import,
)
from academic_radar.governance import (
    apply_cleanup_preview,
    governance_stats,
    preview_cleanup,
    publication_decision,
)
from academic_radar.storage import connect, upgrade_database, utc_now


class GovernanceEnrichmentTests(unittest.TestCase):
    def add_paper(self, db_path: Path, identity: str = "doi:10.1/x", abstract: str = "") -> None:
        upgrade_database(db_path)
        db = connect(db_path)
        with db:
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, identity.removeprefix("doi:"), "A research paper", abstract, "Journal", "2026-01-01",
                 "https://doi.org/" + identity.removeprefix("doi:"), "[]", "now", "now"),
            )
        db.close()

    def test_publication_allowlist_maps_journal_and_conference(self):
        journal = publication_decision("Study", "Journal", "journal-article", "crossref")
        conference = publication_decision("Study", "Proceedings", "proceedings-article", "crossref")
        self.assertEqual((journal["publication_type"], journal["eligibility_status"]), ("Journal Article", "eligible"))
        self.assertEqual((conference["publication_type"], conference["eligibility_status"]), ("Conference Paper", "eligible"))

    def test_negative_title_evidence_overrules_generic_article_type(self):
        for title in (
            "Editorial Board",
            "Extended Abstract: Study",
            "Corrigendum to Study",
            "A Commentary on a Research Article",
        ):
            with self.subTest(title=title):
                result = publication_decision(title, "Journal", "journal-article", "crossref")
                self.assertEqual(result["eligibility_status"], "excluded")
                self.assertTrue(any(item["kind"] == "title_rule" for item in result["evidence"]))

    def test_unknown_type_is_quarantined(self):
        result = publication_decision("Research-looking title", "Unknown", "", "")
        self.assertEqual(result["eligibility_status"], "quarantine")

    def test_specific_publisher_type_overrules_generic_crossref_article(self):
        result=publication_decision("A title","Journal","Correspondence","publisher-official","journal")
        self.assertEqual((result["publication_type"],result["eligibility_status"]),("Letter","excluded"))

    def test_publisher_description_is_not_misrepresented_as_abstract(self):
        class Client:
            def request(self,*args,**kwargs):
                html=b'''<meta name="citation_title" content="A research paper">
                <meta name="dc.description" content="A promotional search description">
                <meta name="citation_article_type" content="Correspondence">'''
                return html,"https://publisher.example/article","text/html"
        result=lookup_publisher(None,{"title":"A research paper","url":"https://publisher.example/article"},Client())
        self.assertEqual(result["abstract"],"")
        self.assertEqual(result["publication_type_raw"],"Correspondence")

    def test_cleanup_preview_has_verified_backup_and_is_recoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db_path = root / "papers.sqlite3"
            self.add_paper(db_path)
            db = connect(db_path)
            with db:
                db.execute("""UPDATE papers SET title='Editorial Board',publication_type_raw='journal-article',
                  publication_type_source='crossref'""")
            db.close()
            preview = preview_cleanup(db_path, root, 0.62)
            self.assertTrue(Path(preview["backup"]).exists())
            self.assertEqual(preview["integrity"], "ok")
            self.assertIn("db restore", preview["restore"])
            applied = apply_cleanup_preview(db_path, root, Path(preview["report_path"]))
            self.assertEqual(applied["after"]["excluded"], 1)

    def test_cleanup_reuses_saved_openalex_source_kind_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db_path = root / "papers.sqlite3"
            self.add_paper(db_path)
            db = connect(db_path)
            with db:
                db.execute("""UPDATE papers SET publication_type='Journal Article',
                  publication_type_raw='article',publication_type_source='openalex',
                  eligibility_status='eligible',publication_type_evidence_json=?""",
                  (json.dumps([
                      {"kind": "metadata_type", "source": "openalex", "value": "article"},
                      {"kind": "source_type", "source": "openalex", "value": "journal"},
                  ]),))
            db.close()
            preview = preview_cleanup(db_path, root, 0.62)
            self.assertEqual(preview["planned"], {
                "eligible": 1, "excluded": 0, "quarantine": 0, "reasons": {},
            })

    def test_multisource_enrichment_continues_after_failure_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path)
            def failed(db, paper, client):
                raise TimeoutError("temporary")
            def found(db, paper, client):
                return {"abstract": "A" * 120, "source_name": "europe-pmc", "source_url": "https://europepmc.org/article",
                        "evidence_type": "europe_pmc_record", "publication_type_raw": "journal-article",
                        "publication_type_source": "europe-pmc"}
            with patch("academic_radar.enrichment.PROVIDERS", [("crossref", failed), ("europe_pmc", found)]):
                result = enrich_abstracts(db_path, {}, retry=True)
            self.assertEqual(result["updated"], 1)
            db = connect(db_path)
            paper = db.execute("SELECT abstract_source,abstract_source_url,needs_rescreen,eligibility_status FROM papers").fetchone()
            attempts = db.execute("SELECT provider,status FROM abstract_attempts ORDER BY id").fetchall()
            db.close()
            self.assertEqual(tuple(paper), ("europe-pmc", "https://europepmc.org/article", 1, "eligible"))
            self.assertEqual([tuple(row) for row in attempts], [("crossref", "failed"), ("europe_pmc", "found")])

    def test_semantic_scholar_batch_uses_one_request_and_exact_doi_mapping(self):
        client=MetadataClient({})
        payload=[{"title":"A research paper","abstract":"Verified abstract","publicationTypes":["JournalArticle"]},None]
        with patch.object(client,"request",return_value=(json.dumps(payload).encode(),"https://api.semanticscholar.org/graph/v1/paper/batch","application/json")) as request:
            prime_semantic_scholar_batch(client,[{"doi":"10.1/x"},{"doi":"10.1/y"},{"doi":"10.1/x"}])
        self.assertEqual(request.call_count,1)
        result=lookup_semantic_scholar(None,{"doi":"10.1/x","title":"A research paper"},client)
        self.assertEqual(result["abstract"],"Verified abstract")
        self.assertIsNone(lookup_semantic_scholar(None,{"doi":"10.1/y","title":"Missing"},client))

    def test_enrichment_is_idempotent_and_does_not_overwrite_complete_record(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path, abstract="Original complete abstract")
            db = connect(db_path)
            with db:
                db.execute("""UPDATE papers SET publication_type='Journal Article',publication_type_raw='journal-article',
                  publication_type_source='crossref',eligibility_status='eligible'""")
            db.close()
            with patch("academic_radar.enrichment.PROVIDERS") as providers:
                result = enrich_abstracts(db_path, {})
            providers.assert_not_called()
            self.assertEqual(result["checked"], 0)

    def test_enrichment_and_manual_task_skip_excluded_non_papers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); db_path=root/"papers.sqlite3"; self.add_paper(db_path)
            db=connect(db_path)
            with db:
                db.execute("UPDATE papers SET eligibility_status='excluded',publication_type='Editorial',exclusion_reason='编辑性内容'")
            db.close()
            with patch("academic_radar.enrichment.PROVIDERS") as providers:
                result=enrich_abstracts(db_path,{})
            self.assertEqual(result["checked"],0); providers.assert_not_called()
            package=export_missing_task_package(db_path,root/"missing.json")
            self.assertEqual(package["count"],0)

    def test_unresolved_abstract_has_honest_failure_reason(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path)
            with patch("academic_radar.enrichment.PROVIDERS", [("crossref", lambda *_: None)]):
                result = enrich_abstracts(db_path, {}, retry=True)
            self.assertEqual(result["unresolved"], 1)
            db = connect(db_path)
            reason = db.execute("SELECT abstract_failure_reason FROM papers").fetchone()[0]
            db.close()
            self.assertIn("公开渠道", reason)

    def test_running_task_prevents_duplicate_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path)
            db = connect(db_path)
            with db:
                db.execute("""INSERT INTO task_runs(task_id,task_type,status,created_at,started_at)
                  VALUES('running','abstract_enrichment','running',?,?)""", (utc_now(), utc_now()))
            db.close()
            with self.assertRaisesRegex(RuntimeError, "已经在运行"):
                enrich_abstracts(db_path, {})

    def test_missing_task_package_contains_required_evidence_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db_path = root / "papers.sqlite3"; self.add_paper(db_path)
            output = root / "missing.json"
            result = export_missing_task_package(db_path, output)
            payload = json.loads(output.read_text())
            self.assertEqual(result["count"], 1)
            self.assertIn("evidence_type", payload["required_import_fields"])
            self.assertIn("绝不", payload["codex_prompt"])

    def test_manual_import_preview_validates_and_apply_marks_rescreen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db_path = root / "papers.sqlite3"; self.add_paper(db_path)
            source = root / "results.json"
            source.write_text(json.dumps([{
                "identity": "doi:10.1/x", "abstract": "Verified abstract text. " * 8,
                "source_name": "Publisher", "source_url": "https://publisher.example/paper",
                "retrieved_at": "2026-07-14T08:00:00+00:00", "evidence_type": "publisher_metadata",
            }]), encoding="utf-8")
            preview = preview_manual_import(db_path, source)
            self.assertEqual(preview["counts"], {"success": 1, "skipped": 0, "failed": 0})
            result = apply_manual_import(db_path, preview)
            self.assertEqual(result["updated"], 1)
            db = connect(db_path)
            row = db.execute("SELECT abstract_source_url,needs_rescreen FROM papers").fetchone()
            db.close()
            self.assertEqual(tuple(row), ("https://publisher.example/paper", 1))

    def test_manual_import_rejects_truncated_and_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db_path = root / "papers.sqlite3"; self.add_paper(db_path)
            item = {"identity": "doi:10.1/x", "abstract": "short...", "source_name": "X",
                    "source_url": "not-a-url", "retrieved_at": "bad", "evidence_type": "snippet"}
            source = root / "bad.json"; source.write_text(json.dumps([item, item]), encoding="utf-8")
            preview = preview_manual_import(db_path, source)
            self.assertEqual(preview["counts"]["success"], 0)
            self.assertEqual(preview["counts"]["failed"], 2)

    def test_governance_stats_excludes_low_score_from_visible_library(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path, abstract="Abstract")
            db = connect(db_path)
            with db:
                db.execute("UPDATE papers SET publication_type='Journal Article',eligibility_status='eligible'")
                db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at)
                  VALUES('doi:10.1/x','p','codex-agent','m',0,0.3,'now')""")
            db.close()
            stats = governance_stats(db_path, 0.62)
            self.assertEqual((stats["below_threshold"], stats["visible"]), (1, 0))

    def test_below_threshold_count_ignores_type_excluded_records(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"; self.add_paper(db_path, abstract="Abstract")
            db = connect(db_path)
            with db:
                db.execute("UPDATE papers SET publication_type='Comment',eligibility_status='excluded'")
                db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at)
                  VALUES('doi:10.1/x','p','codex-agent','m',0,0.1,'now')""")
            db.close()
            stats = governance_stats(db_path, 0.62)
            self.assertEqual((stats["below_threshold"], stats["excluded"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
