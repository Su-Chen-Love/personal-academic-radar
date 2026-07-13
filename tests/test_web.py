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
                "INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                self.assertTrue(client.get("/healthz").json()["ok"])

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

    def test_source_addition_requires_preview_and_preserves_config_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app, _, _ = self.make_app(root)
            fake_preview = {"source": {}, "total_results": 1,
                            "samples": [{"title": "Preview paper", "venue": "New Venue", "doi": "10.1/new"}],
                            "since": "2026-07-01"}
            with TestClient(app) as client, patch("academic_radar.web.preview_source", return_value=fake_preview):
                response = client.post(
                    "/sources/preview",
                    data={"csrf_token": app.state.csrf_token, "name": "New Venue", "type": "crossref",
                          "issn": "9999-9999", "openalex_id": "S9"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("Preview paper", response.text)
                token = next(iter(app.state.pending_sources))
                response = client.post(
                    "/sources/confirm",
                    data={"csrf_token": app.state.csrf_token, "token": token}, follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
            config_text = (root / "config.toml").read_text()
            self.assertIn('name = "New Venue"', config_text)
            self.assertIn('issn = "9999-9999"', config_text)
            self.assertTrue(list(root.glob("config.toml.backup-*")))


if __name__ == "__main__":
    unittest.main()
