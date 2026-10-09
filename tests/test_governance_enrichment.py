import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from academic_radar.enrichment import (
    MetadataClient,
    ProviderTemporarilyUnavailable,
    apply_manual_import,
    clean_abstract,
    enrich_abstracts,
    export_missing_task_package,
    lookup_elsevier,
    lookup_openalex,
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
    should_preserve_publication_metadata,
)
from academic_radar.storage import connect, upgrade_database, utc_now


class GovernanceEnrichmentTests(unittest.TestCase):
    def test_memorial_with_life_dates_is_excluded_without_excluding_memory_research(self):
        decision = publication_decision("In memory of Professor Colin Laurence Eden, 1943-2026", raw_type="journal-article")
        self.assertEqual((decision["eligibility_status"], decision["publication_type"]), ("excluded", "Memorial"))
        self.assertEqual(publication_decision("In memory of past choices: human decision making", raw_type="journal-article")["eligibility_status"], "eligible")

    def test_placeholder_abstract_is_not_original_evidence(self):
        self.assertEqual(clean_abstract("<p>International audience</p>"),"")
        self.assertEqual(clean_abstract("No abstract available"),"")
        self.assertEqual(clean_abstract("The study examined an international audience."),"The study examined an international audience.")

    def test_openalex_requires_exact_doi_before_using_abstract(self):
        class Client:
            user_agent="test"
            def json(self,*args):
                return {"doi":"https://doi.org/10.1/other","title":"Matching paper title",
                        "abstract_inverted_index":{"Unsafe":[0]}},"https://api.openalex.org/works/example"
        self.assertIsNone(lookup_openalex(None,{"doi":"10.1/expected","title":"Matching paper title"},Client()))

    def test_budget_preserves_progress_and_defers_remaining_without_false_failure(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td)/"papers.sqlite3"
            self.add_paper(db_path)
            def exhaust(db, paper, client):
                client.deadline = 0
                return None
            with patch("academic_radar.enrichment.PROVIDERS", [("crossref", exhaust), ("publisher", lambda *_: None)]):
                result = enrich_abstracts(db_path, {})
            self.assertEqual(result['status'], 'partial')
            self.assertTrue(result['budget_exhausted'])
            self.assertEqual(result['checked'], 0)
            self.assertEqual(result['deferred'], 1)
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT status FROM task_runs").fetchone()[0], 'partial')
                self.assertIsNone(db.execute("SELECT abstract_failure_reason FROM papers").fetchone()[0])

    def test_rate_limited_provider_is_paused_for_the_rest_of_the_run(self):
        client = MetadataClient({"collection": {"max_retries": 0}})
        limited = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch("urllib.request.urlopen", side_effect=limited) as request:
            with self.assertRaises(urllib.error.HTTPError):
                client.request("openalex", "https://example.test")
            with self.assertRaises(ProviderTemporarilyUnavailable):
                client.request("openalex", "https://example.test/next")
        self.assertEqual(request.call_count, 1)

    def test_central_transport_outage_does_not_repeat_for_every_doi(self):
        client=MetadataClient({"collection":{"max_retries":0}})
        with patch("urllib.request.urlopen",side_effect=urllib.error.URLError("TLS EOF")) as request:
            with self.assertRaises(urllib.error.URLError):client.request("pubmed","https://example.test/one")
            with self.assertRaises(ProviderTemporarilyUnavailable):client.request("pubmed","https://example.test/two")
        self.assertEqual(request.call_count,1)

    def test_metadata_404_is_not_misreported_as_provider_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"papers.sqlite3";self.add_paper(path)
            def absent(*_):raise urllib.error.HTTPError("https://example.test",404,"not found",{},None)
            with patch("academic_radar.enrichment.PROVIDERS",[("openalex",absent)]):
                result=enrich_abstracts(path,{})
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT status FROM abstract_attempts").fetchone()[0],"not_found")
                self.assertNotIn("HTTPError",db.execute("SELECT abstract_failure_reason FROM papers").fetchone()[0])

    def test_excluded_type_does_not_consume_more_enrichment_requests(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"papers.sqlite3";self.add_paper(path)
            def exclude(*_):
                return {"abstract":"","source_name":"crossref","publication_type_raw":"editorial",
                        "publication_type_source":"crossref","source_url":"https://example.test"}
            with patch("academic_radar.enrichment.PROVIDERS",[("crossref",exclude),("pubmed",lambda *_:self.fail("Excluded paper must stop enrichment"))]):
                result=enrich_abstracts(path,{})
            self.assertEqual(result["unresolved"],0)
            with connect(path) as db:self.assertEqual(db.execute("SELECT eligibility_status FROM papers").fetchone()[0],"excluded")

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
            "Prelim p. 2; First issue - Editorial Board",
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

    def test_research_on_commentary_and_error_correction_is_not_excluded(self):
        for title in (
            "Effects of Human-AI Voice Interaction in Sports Commentary on Perceived Credibility, Emotional Response, and Cognitive Recall",
            "Correction of AI Errors Through User Feedback",
        ):
            with self.subTest(title=title):
                result = publication_decision(title, "Journal", "journal-article", "crossref")
                self.assertEqual(result["eligibility_status"], "eligible")
        for title in (
            "Correction", "Correction: Original Study", "Correction to Original Study",
            "From Shadow AI to Governed AI Review: A Commentary on an Original Study",
            "Commentary on a Field Experiment",
        ):
            with self.subTest(title=title):
                self.assertEqual(publication_decision(title, "Journal", "journal-article", "crossref")["eligibility_status"], "excluded")

    def test_publication_evidence_precedence_allows_correcting_bad_aggregator_type(self):
        self.assertTrue(should_preserve_publication_metadata("crossref", "eligible", "openalex", "excluded"))
        self.assertFalse(should_preserve_publication_metadata("openalex", "excluded", "crossref", "eligible"))
        self.assertTrue(should_preserve_publication_metadata("publisher-official", "excluded", "crossref", "eligible"))
        self.assertTrue(should_preserve_publication_metadata("elsevier-official-api", "eligible", "openalex", "excluded"))
        self.assertTrue(should_preserve_publication_metadata("crossref", "eligible", "", "quarantine"))
        self.assertFalse(should_preserve_publication_metadata("crossref", "quarantine", "openalex", "eligible"))

    def test_enrichment_records_weak_type_conflict_and_continues_to_original_abstract(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "papers.sqlite3"
            self.add_paper(path)
            with connect(path) as db:
                db.execute("""UPDATE papers SET publication_type='Journal Article',publication_type_raw='journal-article',
                  publication_type_source='crossref',eligibility_status='eligible',publication_type_evidence_json=?""",
                  (json.dumps([{"kind": "metadata_type", "source": "crossref", "value": "journal-article"}]),))
            weak = {"abstract": "", "source_name": "openalex", "publication_type_raw": "paratext",
                    "publication_type_source": "openalex", "source_url": "https://api.openalex.org/works/x"}
            original = {"abstract": "The original paper analyzes how index inaccuracy affects farmers' optimal subsidies.",
                        "source_name": "publisher-official", "publication_type_raw": "journal-article",
                        "publication_type_source": "publisher-official", "source_url": "https://publisher.example/article"}
            with patch("academic_radar.enrichment.PROVIDERS", [("openalex", lambda *_: weak), ("publisher", lambda *_: original)]):
                result = enrich_abstracts(path, {})
            self.assertEqual(result["updated"], 1)
            with connect(path) as db:
                row = db.execute("SELECT * FROM papers").fetchone()
                self.assertEqual((row["eligibility_status"], row["publication_type_source"]), ("eligible", "publisher-official"))
                evidence = json.loads(row["publication_type_evidence_json"])
                self.assertTrue(any(e.get("source") == "openalex" and e.get("value") == "paratext" and e["kind"] == "metadata_conflict" for e in evidence))

    def publication_review_fixture(self, root):
        path = root / "papers.sqlite3"
        self.add_paper(path, "doi:10.1287/mnsc.2024.05008")
        title = "Index-Based Yield Protection for Smallholder Farmers"
        with connect(path) as db:
            db.execute("""UPDATE papers SET title=?,publication_type='Front/Back Matter',publication_type_raw='paratext',
              publication_type_source='openalex',eligibility_status='excluded',exclusion_reason='前置或后置材料'""", (title,))
        review = {"identity": "doi:10.1287/mnsc.2024.05008", "doi": "10.1287/mnsc.2024.05008", "title": title,
                  "raw_type": "journal-article", "source": "publisher-official", "source_kind": "journal",
                  "source_url": "https://pubsonline.informs.org/doi/10.1287/mnsc.2024.05008",
                  "verified_at": "2026-10-09T05:40:00+00:00"}
        return path, review

    def test_reviewed_original_types_restore_through_backed_up_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, review = self.publication_review_fixture(root)
            preview = preview_cleanup(path, root, 0.70, publication_reviews=[review])
            self.assertEqual(preview["planned"]["eligible"], 1)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT eligibility_status FROM papers").fetchone()[0], "excluded")
            applied = apply_cleanup_preview(path, root, Path(preview["report_path"]))
            with connect(path) as db:
                row = db.execute("SELECT * FROM papers").fetchone()
                self.assertEqual((row["publication_type_raw"], row["publication_type_source"], row["eligibility_status"], row["needs_rescreen"]),
                                 ("journal-article", "publisher-official", "eligible", 1))
                self.assertTrue(any(e["kind"] == "metadata_conflict" and e["value"] == "paratext" for e in json.loads(row["publication_type_evidence_json"])))
            import sqlite3
            with sqlite3.connect(applied["backup"]) as db:
                self.assertEqual(db.execute("SELECT eligibility_status FROM papers").fetchone()[0], "excluded")
            again = preview_cleanup(path, root, 0.70)
            self.assertTrue(any(e["kind"] == "verified_record" for e in again["items"][0]["decision"]["evidence"]))

    def test_cleanup_rejects_changed_or_unmatched_review_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, review = self.publication_review_fixture(root)
            with self.assertRaisesRegex(ValueError, "标题不匹配"):
                preview_cleanup(path, root, 0.70, publication_reviews=[dict(review, title="Another paper")])
            with self.assertRaisesRegex(ValueError, "身份不存在或重复"):
                preview_cleanup(path, root, 0.70, publication_reviews=[review, review])
            with self.assertRaisesRegex(ValueError, "原始证据"):
                preview_cleanup(path, root, 0.70, publication_reviews=[dict(review, source="openalex")])
            preview = preview_cleanup(path, root, 0.70, publication_reviews=[review])
            changed = json.loads(Path(preview["report_path"]).read_text())
            changed["publication_reviews"][0]["source_url"] = "https://publisher.example/changed"
            Path(preview["report_path"]).write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "核查证据在预览后改变"):
                apply_cleanup_preview(path, root, Path(preview["report_path"]))

    def test_specific_publisher_type_overrules_generic_crossref_article(self):
        result=publication_decision("A title","Journal","Correspondence","publisher-official","journal")
        self.assertEqual((result["publication_type"],result["eligibility_status"]),("Letter","excluded"))
        result=publication_decision("A research highlight","Journal","News & Views","publisher-official","journal")
        self.assertEqual((result["publication_type"],result["eligibility_status"]),("News","excluded"))

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

    def test_elsevier_official_api_uses_exact_doi_and_title(self):
        class Client:
            elsevier_api_key = "configured"
            def request(self,*args,**kwargs):
                payload={"full-text-retrieval-response":{"coredata":{
                    "prism:doi":"10.1016/j.ejor.2026.01.002",
                    "dc:title":"Optimal insurance design",
                    "dc:description":"We derive an optimal insurance contract under distortion risk measures and a variance constraint.",
                }}}
                return json.dumps(payload).encode(),"https://api.elsevier.com/content/article/doi/example","application/json"
        result=lookup_elsevier(None,{
            "doi":"10.1016/j.ejor.2026.01.002","title":"Optimal insurance design"
        },Client())
        self.assertIn("optimal insurance contract",result["abstract"])
        self.assertEqual(result["evidence_type"],"elsevier_api_record")

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
