import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.engagement import (
    active_profile,
    confirm_profile,
    create_profile_draft,
    feedback_examples,
    seed_active_profile,
    set_feedback,
)
from academic_radar.storage import connect, upgrade_database


class EngagementTests(unittest.TestCase):
    def add_paper(self, db_path: Path, identity: str = "doi:10.1/example") -> None:
        upgrade_database(db_path)
        db = connect(db_path)
        with db:
            db.execute(
                """INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, "10.1/example", "Example paper", "Abstract", "Venue", "2026-01-01",
                 "https://doi.org/10.1/example", "[]", "now", "now"),
            )
        db.close()

    def test_feedback_requires_reason_and_keeps_audit_history(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "papers.sqlite3"
            self.add_paper(db_path)
            with self.assertRaisesRegex(ValueError, "reason"):
                set_feedback(db_path, "doi:10.1/example", "interested", "", False, "unread")
            set_feedback(db_path, "doi:10.1/example", "interested", "Transfers directly", True, "read_later")
            set_feedback(db_path, "doi:10.1/example", "not_interested", "Wrong empirical setting", False, "read")
            examples = feedback_examples(db_path)
            self.assertEqual(examples[0]["interest"], "not_interested")
            db = connect(db_path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 2)
            db.close()

    def test_profile_draft_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "papers.sqlite3"
            profile_file = root / "research-profile.md"
            profile_file.write_text("original", encoding="utf-8")
            original = seed_active_profile(db_path, "original")
            draft = create_profile_draft(db_path, "revised", "Add a boundary")
            self.assertEqual(active_profile(db_path)["id"], original["id"])
            self.assertEqual(profile_file.read_text(), "original")
            confirmed = confirm_profile(db_path, draft["id"], profile_file)
            self.assertEqual(confirmed["status"], "active")
            self.assertEqual(active_profile(db_path)["id"], draft["id"])
            self.assertEqual(profile_file.read_text(), "revised")


if __name__ == "__main__":
    unittest.main()
