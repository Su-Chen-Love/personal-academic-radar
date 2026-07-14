import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
    from academic_radar.web import create_app
except ModuleNotFoundError:  # Allows core tests without optional web dependencies.
    TestClient = None

from academic_radar.engagement import seed_active_profile
from academic_radar.storage import connect, upgrade_database


@unittest.skipIf(TestClient is None, "web test dependencies are not installed")
class WebTests(unittest.TestCase):
    def make_app(self, root: Path):
        config = root / "config.toml"
        config.write_text(
            'state_dir = "."\nprofile_file = "research-profile.md"\n'
            '[[sources]]\nname = "Test Venue"\ntype = "crossref"\nissn = "1234-5678"\n',
            encoding="utf-8",
        )
        profile = root / "research-profile.md"
        profile.write_text("confirmed profile", encoding="utf-8")
        db_path = root / "papers.sqlite3"
        upgrade_database(db_path)
        active = seed_active_profile(db_path, "confirmed profile")
        db = connect(db_path)
        with db:
            db.execute(
                """INSERT INTO papers(
                identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("doi:10.1/test", "10.1/test", "A useful paper", "Abstract", "Test Venue",
                 "2026-07-13", "https://doi.org/10.1/test", "[]", "now", "now"),
            )
            db.execute(
                """INSERT INTO screenings(
                identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,
                screened_at,profile_version_id,feedback_snapshot_json,run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("doi:10.1/test", active["profile_hash"], "codex-agent", "test", 1, .9,
                 "Direct match", "[]", .9, "2026-07-13T08:00:00+00:00", active["id"], "[]", "run"),
            )
            db.execute("""UPDATE papers SET publication_type='Journal Article',publication_type_raw='journal-article',
              publication_type_source='crossref',eligibility_status='eligible' WHERE identity='doi:10.1/test'""")
        db.close()
        return create_app(config), db_path, profile

    def test_all_six_pages_and_health_render(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                for path in ("/", "/library", "/sources", "/profile", "/feedback", "/status"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn("个人学术雷达", response.text)
                    self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
                    if path == "/": self.assertIn("app.js?v=0.8.0", response.text)
                self.assertTrue(client.get("/healthz").json()["ok"])

    def test_empty_legacy_state_is_repaired_and_all_pages_render(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.toml").write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "Test Venue"\ntype = "crossref"\nissn = "1234-5678"\n',
                encoding="utf-8",
            )
            (root / "research-profile.md").write_text("legacy profile", encoding="utf-8")
            app = create_app(root / "config.toml")
            with TestClient(app) as client:
                for path in ("/", "/library", "/sources", "/profile", "/feedback", "/status"):
                    self.assertEqual(client.get(path).status_code, 200, path)
            db = connect(root / "papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_versions WHERE status='active'").fetchone()[0], 1)
            db.close()

    def test_feedback_form_persists_and_is_csrf_protected(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                denied = client.post("/feedback", data={"identity": "doi:10.1/test"})
                self.assertEqual(denied.status_code, 403)
                response = client.post(
                    "/feedback",
                    data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                          "interest": "interested", "reason": "Direct transfer", "favorite": "on",
                          "reading_status": "read_later", "return_to": "/library"},
                    follow_redirects=False,
                )
                self.assertEqual((response.status_code, response.headers["location"]), (303, "/library"))
            db = connect(db_path)
            saved = db.execute("SELECT interest,reason,favorite,reading_status FROM paper_feedback").fetchone()
            self.assertEqual(tuple(saved), ("interested", "Direct transfer", 1, "read_later"))
            db.close()

    def test_validation_error_is_a_friendly_html_page(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/feedback",
                    data={"csrf_token": app.state.csrf_token, "identity": "doi:10.1/test",
                          "interest": "interested", "reason": "", "reading_status": "unread"},
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("这次操作没有保存", response.text)
            self.assertIn("数据已保留", response.text)

    def test_profile_web_flow_keeps_draft_inactive_until_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, profile_file = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/profile/draft",
                    data={"csrf_token": app.state.csrf_token, "content": "revised profile",
                          "summary": "Tighten boundaries"}, follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(profile_file.read_text(), "confirmed profile")
                db = connect(db_path)
                draft_id = db.execute("SELECT id FROM profile_versions WHERE status='draft'").fetchone()[0]
                db.close()
                response = client.post(
                    "/profile/confirm",
                    data={"csrf_token": app.state.csrf_token, "version_id": str(draft_id)},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(profile_file.read_text(), "revised profile")

    def test_direct_profile_assistant_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post(
                    "/profile/assist",
                    data={"csrf_token": app.state.csrf_token, "materials": "paper notes",
                          "summary": "Generate from notes"},
                    follow_redirects=False,
                )
                page=client.get("/profile")
            self.assertEqual(response.status_code,404)
            self.assertNotIn("备用 API",page.text)

    def test_source_addition_requires_preview_and_preserves_config_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            config_path = root / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8") + '\n[custom]\nkeep_after_sources = "yes"\n',
                encoding="utf-8",
            )
            # Reload after adding a section that must survive source replacement.
            app = create_app(config_path)
            fake_preview = {"source": {}, "total_results": 1,
                            "samples": [{"title": "Preview paper", "venue": "New Venue", "doi": "10.1/new"}],
                            "since": "2026-07-01"}
            candidate={"candidate_id":"c1","name":"New Venue","config_type":"crossref","issn":"9999-9999","openalex_id":"S9"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates",return_value=[candidate]), \
                 patch("academic_radar.web.preview_source", return_value=fake_preview):
                found=client.get("/api/sources/search?q=New")
                self.assertEqual(found.status_code,200)
                response = client.post("/api/sources/preview",json={"candidate_id":"c1"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
                token = next(iter(app.state.pending_sources))
                response = client.post("/api/sources/confirm",json={"token":token},
                    headers={"X-CSRF-Token":app.state.csrf_token})
                self.assertEqual(response.status_code, 200)
            config_text = (root / "config.toml").read_text()
            self.assertIn('name = "New Venue"', config_text)
            self.assertIn('issn = "9999-9999"', config_text)
            self.assertIn('[custom]\nkeep_after_sources = "yes"', config_text)
            self.assertTrue(list(root.glob("config.toml.backup-*")))

    def test_source_name_matching_renders_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            candidate = {"candidate_id":"c1","name": "Candidate Journal", "issn": "1111-2222",
                         "publisher": "Publisher", "openalex_id": "S1","config_type":"crossref"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates", return_value=[candidate]):
                response = client.get("/api/sources/search?q=Candidate")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"][0]["name"],"Candidate Journal")
                self.assertEqual(client.post("/sources/match").status_code,404)

    def test_fulltext_upload_copies_pdf_to_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, db_path, _ = self.make_app(root)
            boundary = "----radar"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{app.state.csrf_token}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"identity\"\r\n\r\ndoi:10.1/test\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"return_to\"\r\n\r\n/library\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"paper.pdf\"\r\n"
                "Content-Type: application/pdf\r\n\r\n%PDF-1.4 test\r\n"
                f"--{boundary}--\r\n"
            ).encode("utf-8")
            with TestClient(app) as client:
                response = client.post(
                    "/fulltext",
                    content=body,
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
            db = connect(db_path)
            saved = db.execute("SELECT stored_path,original_name FROM fulltext_files").fetchone()
            self.assertEqual(saved["original_name"], "paper.pdf")
            self.assertTrue(Path(saved["stored_path"]).exists())
            db.close()

    def test_library_sort_filters_pagination_and_pdf_entry_render_together(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                client.post("/api/favorite",json={"identity":"doi:10.1/test","favorite":True},
                            headers={"X-CSRF-Token":app.state.csrf_token})
                response = client.get("/library?sort=score_asc&favorite=yes&q=useful&page_no=1")
            self.assertEqual(response.status_code, 200)
            self.assertIn("匹配度：低到高", response.text)
            self.assertIn("导入全文 PDF", response.text)
            self.assertEqual(response.text.count('action="/fulltext"'), 1)
            self.assertIn("data-abstract-toggle", response.text)

    def test_favorite_api_is_independent_and_immediate(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                response = client.post("/api/favorite", json={"identity":"doi:10.1/test","favorite":True},
                                       headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code, 200)
            db=connect(db_path); saved=db.execute("SELECT favorite,interest FROM paper_feedback").fetchone(); db.close()
            self.assertEqual(tuple(saved),(1,None))

    def test_today_uses_selected_new_from_latest_import_and_compact_details(self):
        with tempfile.TemporaryDirectory() as td:
            app, db_path, _ = self.make_app(Path(td))
            db=connect(db_path)
            with db:
                db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at,finished_at)
                  VALUES('run','agent-export','succeeded','now','now')""")
                active=db.execute("SELECT profile_hash,id FROM profile_versions WHERE status='active'").fetchone()
                db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,exported_count,imported_count,created_at,imported_at,profile_version_id)
                  VALUES('run',?,'imported',1,1,'now','now',?)""",(active[0],active[1]))
                db.execute("INSERT INTO run_papers VALUES('run','doi:10.1/test','selected_new')")
            db.close()
            with TestClient(app) as client: response=client.get("/")
            self.assertIn("A useful paper",response.text)
            self.assertIn("展开详情",response.text)
            self.assertNotIn('action="/fulltext"',response.text)
            self.assertIn("AI",response.text)

    def test_feedback_page_is_interactive_without_change_history(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                client.post("/feedback",data={"csrf_token":app.state.csrf_token,"identity":"doi:10.1/test",
                    "interest":"interested","reason":"匹配研究问题","reading_status":"read_later"})
                response=client.get("/feedback?interest=interested")
            self.assertIn("查看与编辑",response.text)
            self.assertIn("清除当前反馈",response.text)
            self.assertNotIn("变更历史",response.text)

    def test_source_search_assets_support_debounce_keyboard_and_no_page_post(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            with TestClient(app) as client:
                page=client.get("/sources")
                script=client.get("/static/app.js")
            self.assertNotIn("/sources/match",page.text)
            self.assertIn("320",script.text)
            self.assertIn("ArrowDown",script.text)
            self.assertIn("preventDefault",script.text)
            self.assertIn("aria-activedescendant",script.text)
            self.assertIn("aria-selected",script.text)

    def test_duplicate_source_is_marked_and_cannot_preview(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _ = self.make_app(Path(td))
            candidate={"candidate_id":"same","name":"Test Venue","issn":"1234-5678","config_type":"crossref"}
            with TestClient(app) as client, patch("academic_radar.web.source_candidates",return_value=[candidate]):
                found=client.get("/api/sources/search?q=Test").json()["items"][0]
                blocked=client.post("/api/sources/preview",json={"candidate_id":"same"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertTrue(found["added"])
            self.assertEqual(blocked.status_code,409)

    def test_source_removal_backs_up_config_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); app, db_path, _=self.make_app(root); config=root/"config.toml"
            config.write_text(config.read_text()+"\n[[sources]]\nname = \"Second Venue\"\ntype = \"crossref\"\nissn = \"9999-9999\"\n",encoding="utf-8")
            app=create_app(config)
            db=connect(db_path)
            with db: db.execute("INSERT INTO observations VALUES('doi:10.1/test','Second Venue','now')")
            db.close()
            with TestClient(app) as client:
                response=client.post("/api/sources/remove",json={"name":"Second Venue"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code,200)
            self.assertNotIn('name = "Second Venue"',config.read_text())
            self.assertTrue(list(root.glob("config.toml.backup-*")))
            db=connect(db_path); count=db.execute("SELECT COUNT(*) FROM observations WHERE source='Second Venue'").fetchone()[0]; db.close()
            self.assertEqual(count,1)

    def test_source_removal_failure_keeps_original_config(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); app, _, _=self.make_app(root); config=root/"config.toml"
            config.write_text(config.read_text()+"\n[[sources]]\nname = \"Second Venue\"\ntype = \"crossref\"\nissn = \"9999-9999\"\n",encoding="utf-8")
            original=config.read_text(); app=create_app(config)
            with TestClient(app) as client, patch("academic_radar.web.os.replace",side_effect=OSError("disk")):
                response=client.post("/api/sources/remove",json={"name":"Second Venue"},
                    headers={"X-CSRF-Token":app.state.csrf_token})
            self.assertEqual(response.status_code,500)
            self.assertEqual(config.read_text(),original)

    def test_status_page_has_actionable_buttons_and_no_provider_api(self):
        with tempfile.TemporaryDirectory() as td:
            app, _, _=self.make_app(Path(td))
            with TestClient(app) as client: response=client.get("/status")
            for label in ("立即补全摘要","立即更新文献 / 建立 Codex 队列","重新检查状态"):
                self.assertIn(label,response.text)
            self.assertNotIn("API Key",response.text)
            self.assertNotIn("DeepSeek",response.text)


if __name__ == "__main__":
    unittest.main()
