import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from academic_radar.engagement import seed_active_profile
from academic_radar.operations import verify_installation
from academic_radar.storage import connect, upgrade_database


class OperationsTests(unittest.TestCase):
    def test_verify_reports_actionable_incomplete_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/"config.toml"; profile=root/"research-profile.md"
            profile.write_text("profile",encoding="utf-8")
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            upgrade_database(root/"papers.sqlite3"); seed_active_profile(root/"papers.sqlite3","profile")
            result=verify_installation(config)
            self.assertFalse(result["ok"])
            checks={item["name"]:item for item in result["checks"]}
            self.assertTrue(checks["database_integrity"]["ok"])
            self.assertTrue(checks["confirmed_profile"]["ok"])
            self.assertFalse(checks["latest_semantic_job"]["ok"])


if __name__ == "__main__": unittest.main()
