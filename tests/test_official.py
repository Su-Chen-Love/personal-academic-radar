import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from academic_radar.official import (
    apply_official_import,
    build_official_plan,
    collect_supported_official,
    configure_official_source,
    official_status,
    preview_official_import,
    record_official_failure,
)
from academic_radar.storage import connect, upgrade_database


class OfficialIssueTests(unittest.TestCase):
    def test_ejor_and_transportation_science_have_verified_official_mappings(self):
        ejor = configure_official_source({"name": "EJOR", "type": "crossref", "issn": "0377-2217"})
        trsc = configure_official_source({"name": "Transportation Science", "type": "crossref", "issn": "0041-1655"})
        self.assertEqual(ejor["official_provider"], "Elsevier ScienceDirect")
        self.assertIn("european-journal-of-operational-research", ejor["official_issues_url"])
        self.assertEqual(trsc["official_provider"], "INFORMS PubsOnline")
        self.assertTrue(trsc["official_issues_url"].endswith("/trsc"))

    @patch("academic_radar.official._fetch_json")
    def test_sciencedirect_print_metadata_adapter_uses_latest_two_published_issues(self, fetch):
        fetch.side_effect = [
            {"message": {"items": [
                {"DOI":"10.1016/j.ejor.2026.1","title":["First"],"volume":"332","issue":"1",
                 "published-print":{"date-parts":[[2026,7,1]]},"type":"journal-article","author":[],
                 "link":[{"URL":"https://api.elsevier.com/content/article/PII:S037722172600001X?httpAccept=text/xml"}]},
                {"DOI":"10.1016/j.ejor.2026.2","title":["Second"],"volume":"332","issue":"2",
                 "published-print":{"date-parts":[[2026,7,16]]},"type":"journal-article","author":[],
                 "link":[{"URL":"https://api.elsevier.com/content/article/PII:S037722172600002X?httpAccept=text/xml"}]},
                {"DOI":"10.1016/j.ejor.2026.3","title":["Future"],"volume":"332","issue":"3",
                 "published-print":{"date-parts":[[2026,8,1]]},"type":"journal-article","author":[]},
            ]}},
            {"doi":"https://doi.org/10.1016/j.ejor.2026.2","abstract_inverted_index":{}},
            {"doi":"https://doi.org/10.1016/j.ejor.2026.1","abstract_inverted_index":{}},
        ]
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            upgrade_database(db_path)
            output = Path(td) / "official.json"
            result = collect_supported_official(db_path, {"sources":[{
                "name":"European Journal of Operational Research","type":"crossref","issn":"0377-2217"
            }]}, output)
            issues = json.loads(output.read_text())["sources"][0]["issues"]
        self.assertEqual((result["sources"], result["issues"], result["papers"]), (1, 2, 2))
        self.assertEqual([item["issue_key"] for item in issues], ["volume-332-issue-2", "volume-332-issue-1"])
        self.assertEqual(
            issues[0]["papers"][0]["source_url"],
            "https://www.sciencedirect.com/science/article/pii/S037722172600002X",
        )
        self.assertTrue(issues[0]["papers"][0]["abstract_unavailable_traceable"])
        self.assertIn("exact-DOI OpenAlex", issues[0]["papers"][0]["abstract_failure_reason"])

    def test_plan_routes_verified_journal_and_api_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [
                {"name": "International Journal of Human-Computer Studies", "type": "crossref", "issn": "1071-5819"},
                {"name": "Conference", "type": "crossref-query", "query_container": "Conference"},
            ]}

            plan = build_official_plan(db_path, config)

            self.assertEqual(plan["issue_limit"], 2)
            self.assertRegex(plan["as_of_date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(plan["sources"][0]["provider"], "Elsevier ScienceDirect")
            self.assertIn("sciencedirect.com", plan["sources"][0]["issues_url"])
            self.assertIn("未来卷期", plan["sources"][0]["instructions"])
            self.assertEqual(plan["api_fallback"][0]["source_name"], "Conference")

    def test_configure_unknown_journal_marks_api_bridge(self):
        source = configure_official_source({"name": "Unknown", "type": "crossref", "issn": "9999-9999"})
        self.assertEqual(source["official_status"], "api_fallback")
        self.assertNotIn("official_issues_url", source)

    def test_failure_is_traceable_without_mutating_papers(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "International Journal of Human-Computer Studies",
                "type": "crossref", "issn": "1071-5819",
            }, {"name": "Conference", "type": "crossref-query"}]}

            failure = record_official_failure(
                db_path, config, "International Journal of Human-Computer Studies", "volume-212",
                "https://www.sciencedirect.com/journal/international-journal-of-human-computer-studies/vol/212/suppl/C",
                "论文详情页暂时无法访问",
            )
            status = official_status(db_path, config)

            self.assertEqual(failure["status"], "failed")
            self.assertEqual(status["counts"], {
                "official": 1, "api_fallback": 1, "with_success": 0, "with_failure": 1,
            })
            self.assertEqual(status["sources"][0]["latest_check"]["issue_key"], "volume-212")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
            db.close()

            with self.assertRaises(ValueError):
                record_official_failure(
                    db_path, config, "International Journal of Human-Computer Studies", "bad",
                    "https://example.com/not-official", "blocked",
                )

    def test_supported_nature_collector_builds_strict_two_issue_package(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "Nature Human Behaviour", "type": "crossref", "issn": "2397-3374",
            }]}
            index = """
              <a itemprop='url' content='https://www.nature.com/nathumbehav/volumes/10/issues/7'>
                <span itemprop='datePublished' content='August 2026'></span></a>
              <a itemprop='url' content='https://www.nature.com/nathumbehav/volumes/10/issues/6'>
                <span itemprop='datePublished' content='June 2026'></span></a>
              <a itemprop='url' content='https://www.nature.com/nathumbehav/volumes/10/issues/5'>
                <span itemprop='datePublished' content='May 2026'></span></a>
            """
            article_template = """
              <meta name='dc.type' content='OriginalPaper'>
              <meta name='dc.description' content='A complete official abstract with enough detail for strict validation. A complete official abstract with enough detail.'>
              <meta name='citation_title' content='Verified Nature study'>
              <meta name='citation_doi' content='10.1038/s41562-026-0000{number}-1'>
              <meta name='citation_author' content='A. Author'>
              <meta name='citation_online_date' content='2026/05/01'>
              <meta name='citation_article_type' content='Article'>
            """

            def fake_fetch(url):
                if url.endswith("browse-issues"): return index
                if url.endswith("/issues/6"): return "<a href='/articles/s41562-026-00001-1'>Paper</a>"
                if url.endswith("/issues/5"): return "<a href='/articles/s41562-026-00002-1'>Paper</a>"
                return article_template.format(number="1" if "00001" in url else "2")

            output = root / "nature.json"
            with patch("academic_radar.official._fetch_text", side_effect=fake_fetch):
                result = collect_supported_official(db_path, config, output)
            preview = preview_official_import(db_path, config, output)

            self.assertEqual((result["issues"], result["papers"]), (2, 2))
            self.assertEqual(preview["counts"]["failed"], 0)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["sources"][0]["issues"][0]["issue_key"], "volume-10-issue-6")
            self.assertEqual(payload["sources"][0]["issues"][0]["papers"][0]["publication_type_raw"], "research-article")

    def test_print_metadata_collector_selects_latest_two_published_issues_and_labels_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "Ergonomics", "type": "crossref", "issn": "0014-0139",
            }]}
            long_abstract = "A complete publisher-deposited abstract with sufficient detail for strict validation. " * 3
            items = [
                {
                    "DOI": "10.1080/00140139.2026.7", "title": ["Future issue paper"],
                    "published-print": {"date-parts": [[2026, 8, 1]]}, "volume": "69", "issue": "7",
                    "resource": {"primary": {"URL": "https://www.tandfonline.com/doi/full/10.1080/00140139.2026.7"}},
                },
                {
                    "DOI": "10.1080/00140139.2026.6", "title": ["Current issue paper"],
                    "abstract": long_abstract, "published-print": {"date-parts": [[2026, 6, 3]]},
                    "volume": "69", "issue": "6", "author": [{"given": "A", "family": "Author"}],
                    "resource": {"primary": {"URL": "https://www.tandfonline.com/doi/full/10.1080/00140139.2026.6"}},
                },
                {
                    "DOI": "10.1080/00140139.2026.5", "title": ["Previous issue paper"],
                    "published-print": {"date-parts": [[2026, 5, 4]]}, "volume": "69", "issue": "5",
                    "resource": {"primary": {"URL": "https://www.tandfonline.com/doi/full/10.1080/00140139.2026.5"}},
                },
            ]

            def fake_fetch_json(url):
                if "api.crossref.org/journals/" in url:
                    return {"message": {"items": items}}
                return {
                    "doi": "https://doi.org/10.1080/00140139.2026.5",
                    "abstract_inverted_index": {
                        "A": [0], "complete": [1], "exact": [2], "DOI": [3], "matched": [4],
                        "abstract": [5], "with": [6], "enough": [7], "detail": [8], "for": [9],
                        "strict": [10], "validation": [11], "and": [12], "traceable": [13],
                        "evidence": [14], "from": [15], "the": [16], "metadata": [17],
                        "service": [18], "without": [19], "a": [20], "snippet": [21],
                    },
                }

            output = root / "ergonomics.json"
            with patch("academic_radar.official._fetch_json", side_effect=fake_fetch_json):
                result = collect_supported_official(db_path, config, output)
            preview = preview_official_import(db_path, config, output)
            payload = json.loads(output.read_text())["sources"][0]["issues"]

            self.assertEqual((result["issues"], result["papers"]), (2, 2))
            self.assertEqual(preview["counts"]["failed"], 0)
            self.assertEqual([item["issue_key"] for item in payload], [
                "volume-69-issue-6", "volume-69-issue-5",
            ])
            self.assertEqual(payload[0]["papers"][0]["abstract_evidence_type"], "publisher_deposited_metadata")
            self.assertEqual(payload[1]["papers"][0]["abstract_evidence_type"], "scholarly_metadata")

    def test_strict_import_is_atomic_deduplicated_and_traceable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "International Journal of Human-Computer Studies",
                "type": "crossref",
                "issn": "1071-5819",
            }]}
            result_path = root / "official.json"
            result_path.write_text(json.dumps({"sources": [{
                "source_name": "International Journal of Human-Computer Studies",
                "issues": [{
                    "issue_key": "volume-213",
                    "issue_url": "https://www.sciencedirect.com/journal/international-journal-of-human-computer-studies/vol/213/suppl/C",
                    "papers": [{
                        "doi": "10.1016/j.ijhcs.2026.103825",
                        "title": "Emotional engagement in AI storytelling",
                        "abstract": "This is a verified publisher abstract with enough detail for strict validation. " * 3,
                        "authors": ["A", "B"],
                        "published": "2026-06-01",
                        "source_url": "https://www.sciencedirect.com/science/article/pii/S107158192600100X",
                        "publication_type_raw": "journal-article",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, result_path)
            applied = apply_official_import(db_path, preview)

            self.assertEqual((preview["counts"]["papers"], preview["counts"]["failed"]), (1, 0))
            self.assertEqual((applied["inserted"], applied["papers"]), (1, 1))
            db = connect(db_path)
            paper = db.execute("SELECT * FROM papers").fetchone()
            observation = db.execute("SELECT source FROM observations").fetchone()[0]
            attempt = db.execute("SELECT provider,evidence_type FROM abstract_attempts").fetchone()
            checked = db.execute("SELECT status,article_count FROM official_issue_checks").fetchone()
            db.close()
            self.assertEqual(paper["abstract_source"], "publisher-official")
            self.assertIn("Official issue volume-213", observation)
            self.assertEqual(tuple(attempt), ("official", "official_page"))
            self.assertEqual(tuple(checked), ("succeeded", 1))

            repeated = preview_official_import(db_path, config, result_path)
            self.assertEqual((repeated["counts"]["papers"], repeated["counts"]["skipped"]), (0, 1))

    def test_rejects_incomplete_research_abstract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "International Journal of Human-Computer Studies", "type": "crossref", "issn": "1071-5819",
            }]}
            path = root / "bad.json"
            path.write_text(json.dumps({"sources": [{
                "source_name": "International Journal of Human-Computer Studies",
                "issues": [{
                    "issue_key": "volume-214",
                    "issue_url": "https://www.sciencedirect.com/journal/international-journal-of-human-computer-studies/vol/214/suppl/C",
                    "papers": [{
                        "doi": "10.1016/j.ijhcs.2026.1", "title": "Research paper", "abstract": "short",
                        "source_url": "https://www.sciencedirect.com/science/article/pii/S1",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, path)

            self.assertEqual(preview["counts"]["failed"], 1)
            with self.assertRaises(ValueError):
                apply_official_import(db_path, preview)

    def test_accepts_publisher_deposited_crossref_abstract_with_honest_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "Management Science", "type": "crossref", "issn": "0025-1909",
            }]}
            path = root / "publisher-metadata.json"
            evidence_url = "https://api.crossref.org/works/10.1287%2Fmnsc.2023.04294"
            path.write_text(json.dumps({"sources": [{
                "source_name": "Management Science",
                "issues": [{
                    "issue_key": "volume-72-issue-7",
                    "issue_url": "https://pubsonline.informs.org/toc/mnsc/72/7",
                    "papers": [{
                        "doi": "10.1287/mnsc.2023.04294",
                        "title": "Research paper with publisher-deposited metadata",
                        "abstract": "This complete abstract was deposited by the publisher and is long enough for strict validation. " * 3,
                        "source_url": "https://pubsonline.informs.org/doi/10.1287/mnsc.2023.04294",
                        "abstract_evidence_url": evidence_url,
                        "abstract_evidence_type": "publisher_deposited_metadata",
                        "publication_type_raw": "research-article",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, path)
            applied = apply_official_import(db_path, preview)

            self.assertEqual(preview["counts"]["failed"], 0)
            self.assertEqual(applied["inserted"], 1)
            db = connect(db_path)
            paper = db.execute(
                "SELECT abstract_source,abstract_source_url FROM papers"
            ).fetchone()
            attempt = db.execute(
                "SELECT provider,evidence_type,source_url FROM abstract_attempts"
            ).fetchone()
            check = db.execute("SELECT detail FROM official_issue_checks").fetchone()[0]
            db.close()
            self.assertEqual(paper["abstract_source"], "publisher-deposited-metadata")
            self.assertEqual(paper["abstract_source_url"], evidence_url)
            self.assertEqual((attempt["provider"], attempt["evidence_type"]), (
                "crossref", "publisher_deposited_metadata",
            ))
            self.assertEqual(attempt["source_url"], evidence_url)
            self.assertIn("出版商提交的 Crossref 元数据", check)

    def test_accepts_exact_doi_openalex_abstract_with_honest_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "Ergonomics", "type": "crossref", "issn": "0014-0139",
            }]}
            path = root / "scholarly-metadata.json"
            evidence_url = "https://api.openalex.org/works/https://doi.org/10.1080%2F00140139.2025.1"
            path.write_text(json.dumps({"sources": [{
                "source_name": "Ergonomics",
                "issues": [{
                    "issue_key": "volume-69-issue-6",
                    "issue_url": "https://www.tandfonline.com/toc/terg20/69/6",
                    "papers": [{
                        "doi": "10.1080/00140139.2025.1",
                        "title": "A complete ergonomics research paper",
                        "abstract": "This abstract was recovered by exact DOI matching and is long enough for strict validation. " * 3,
                        "source_url": "https://www.tandfonline.com/doi/full/10.1080/00140139.2025.1",
                        "abstract_evidence_url": evidence_url,
                        "abstract_evidence_type": "scholarly_metadata",
                        "publication_type_raw": "research-article",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, path)
            apply_official_import(db_path, preview)

            db = connect(db_path)
            paper = db.execute("SELECT abstract_source,abstract_source_url FROM papers").fetchone()
            attempt = db.execute("SELECT provider,evidence_type FROM abstract_attempts").fetchone()
            check = db.execute("SELECT detail FROM official_issue_checks").fetchone()[0]
            db.close()
            self.assertEqual(paper["abstract_source"], "scholarly-metadata")
            self.assertEqual(paper["abstract_source_url"], evidence_url)
            self.assertEqual(tuple(attempt), ("openalex", "scholarly_metadata"))
            self.assertIn("OpenAlex 学术元数据", check)

    def test_accepts_traceable_officially_missing_abstract_without_using_highlights(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "International Journal of Human-Computer Studies", "type": "crossref", "issn": "1071-5819",
            }]}
            path = root / "official-missing.json"
            path.write_text(json.dumps({"sources": [{
                "source_name": "International Journal of Human-Computer Studies",
                "issues": [{
                    "issue_key": "volume-214",
                    "issue_url": "https://www.sciencedirect.com/journal/international-journal-of-human-computer-studies/vol/214/suppl/C",
                    "papers": [{
                        "doi": "10.1016/j.ijhcs.2026.2",
                        "title": "Research paper whose publisher page only has highlights",
                        "abstract": "",
                        "abstract_unavailable_official": True,
                        "abstract_failure_reason": "ScienceDirect 详情页只有 Highlights，没有 Abstract 区块",
                        "source_url": "https://www.sciencedirect.com/science/article/pii/S2",
                        "publication_type_raw": "research-article",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, path)
            applied = apply_official_import(db_path, preview)

            self.assertEqual(preview["counts"]["failed"], 0)
            self.assertEqual(applied["inserted"], 1)
            db = connect(db_path)
            paper = db.execute(
                "SELECT abstract,abstract_source,abstract_failure_reason,needs_rescreen FROM papers"
            ).fetchone()
            attempt = db.execute(
                "SELECT status,evidence_type,detail FROM abstract_attempts"
            ).fetchone()
            check = db.execute("SELECT detail FROM official_issue_checks").fetchone()[0]
            db.close()
            self.assertEqual(paper["abstract"], "")
            self.assertEqual(paper["abstract_source"], "missing")
            self.assertEqual(paper["needs_rescreen"], 1)
            self.assertIn("只有 Highlights", paper["abstract_failure_reason"])
            self.assertEqual((attempt["status"], attempt["evidence_type"]), ("not_found", "official_page"))
            self.assertIn("官网明确未提供摘要", check)

    def test_accepts_traceable_temporary_metadata_gap_and_keeps_rescreen_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            upgrade_database(db_path)
            config = {"sources": [{
                "name": "IEEE Transactions on Human-Machine Systems",
                "type": "crossref", "issn": "2168-2291",
            }]}
            path = root / "gap.json"
            path.write_text(json.dumps({"sources": [{
                "source_name": "IEEE Transactions on Human-Machine Systems",
                "issues": [{
                    "issue_key": "volume-56-issue-3",
                    "issue_url": "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=6221037&volume=56&issue=3",
                    "papers": [{
                        "doi": "10.1109/thms.2026.1", "title": "New research paper",
                        "abstract": "", "abstract_unavailable_traceable": True,
                        "abstract_failure_reason": "Publisher page protected and exact-DOI metadata services not indexed yet",
                        "source_url": "https://ieeexplore.ieee.org/document/10000001/",
                        "publication_type_raw": "journal-article",
                    }],
                }],
            }]}), encoding="utf-8")

            preview = preview_official_import(db_path, config, path)
            apply_official_import(db_path, preview)
            db = connect(db_path)
            paper = db.execute("SELECT abstract,needs_rescreen FROM papers").fetchone()
            attempt = db.execute("SELECT status,evidence_type FROM abstract_attempts").fetchone()
            detail = db.execute("SELECT detail FROM official_issue_checks").fetchone()[0]
            db.close()
            self.assertEqual((paper["abstract"], paper["needs_rescreen"]), ("", 1))
            self.assertEqual(tuple(attempt), ("not_found", "verified_metadata_gap"))
            self.assertIn("摘要暂不可得", detail)


if __name__ == "__main__":
    unittest.main()
