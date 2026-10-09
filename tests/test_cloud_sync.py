import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from academic_radar.cloud_sync import (
    _NoCredentialRedirect,
    _synchronize,
    _verified_sites_origin,
    apply_remote_feedback,
    canonical,
    request_json,
    snapshot_records,
)
from academic_radar.storage import connect, upgrade_database


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "papers.sqlite3"
        upgrade_database(self.path)
        self.db = connect(self.path)
        with self.db:
            self.db.execute("""INSERT INTO papers(identity,doi,title,abstract,venue,published,url,
              authors_json,first_seen,updated_at,publication_type,eligibility_status)
              VALUES('doi:10.1/x','10.1/x','A research paper','An original abstract','Journal',
              '2026-10-01','https://doi.org/10.1/x','[]','now','now','Journal Article','eligible')""")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def event(self, **changes):
        value = dict(seq=1, identity="doi:10.1/x", interest="interested", reason="Useful decision-support method",
                     favorite=1, reading_status="read_later", updated_at="2026-10-09T03:00:00Z")
        value.update(changes)
        return value

    def test_feedback_and_cursor_commit_together(self):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES('cloud_feedback_cursor','7')")
        events = [self.event(seq=8), self.event(seq=9, identity="doi:10.1/unknown")]
        with self.assertRaisesRegex(ValueError, "unknown paper"):
            apply_remote_feedback(self.db, events, 9)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()[0], "7")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_feedback").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT needs_rescreen FROM papers").fetchone()[0], 0)

    def test_newer_local_feedback_and_equal_timestamp_win(self):
        apply_remote_feedback(self.db, [self.event(updated_at="2026-10-09T11:00:00+08:00")], 1)
        for stamp in ("2026-10-09T02:59:59Z", "2026-10-09T03:00:00Z"):
            changed = apply_remote_feedback(self.db, [self.event(seq=2, interest="not_interested", reason="Different preference", updated_at=stamp)], 2)
            self.assertEqual(changed, 0)
        self.assertEqual(self.db.execute("SELECT interest FROM paper_feedback").fetchone()[0], "interested")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()[0], "2")
        changed = apply_remote_feedback(self.db, [self.event(seq=3, interest="not_interested", reason="Different preference", updated_at="2026-10-09T03:00:01Z")], 3)
        self.assertEqual(changed, 1)
        self.assertEqual(self.db.execute("SELECT interest FROM paper_feedback").fetchone()[0], "not_interested")

    def test_repeated_pull_does_not_duplicate_history(self):
        first = self.event()
        self.assertEqual(apply_remote_feedback(self.db, [first], 1), 1)
        self.assertEqual(apply_remote_feedback(self.db, [first], 1), 0)
        self.assertEqual(apply_remote_feedback(self.db, [first], 2), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_feedback").fetchone()[0], 1)

    def test_reading_and_favorite_updates_are_not_semantic_feedback(self):
        apply_remote_feedback(self.db, [self.event()], 1)
        with self.db:
            self.db.execute("UPDATE papers SET needs_rescreen=0")
        changed = apply_remote_feedback(self.db, [self.event(seq=2,favorite=0,reading_status="read",updated_at="2026-10-09T03:01:00Z")],2)
        self.assertEqual(changed,1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT needs_rescreen FROM papers").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT reading_status FROM paper_feedback").fetchone()[0],"read")

    def test_invalid_timestamp_on_new_feedback_rolls_back_cursor(self):
        with self.assertRaises(ValueError):
            apply_remote_feedback(self.db, [self.event(updated_at="not-a-date")], 1)
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 0)

    def test_invalid_cursor_or_future_sequence_is_refused(self):
        apply_remote_feedback(self.db, [], 4)
        for events, cursor in (([], 3), ([], True), ([], -1), ([], "5"), ([self.event(seq=6)], 5)):
            with self.assertRaises(ValueError):
                apply_remote_feedback(self.db, events, cursor)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()[0], "4")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 0)

    def test_snapshot_uses_latest_host_screening_for_active_profile(self):
        with self.db:
            self.db.execute("""INSERT INTO profile_versions(profile_hash,content,status,created_at)
              VALUES('active-profile','Current interests','active','now')""")
            for profile, provider, score, stamp, model in (
                ("active-profile", "codex-agent", .99, "2026-10-07", "old"),
                ("active-profile", "codex-agent", .75, "2026-10-08", "current"),
                ("old-profile", "codex-agent", .97, "2026-10-09", "stale"),
                ("active-profile", "keyword", 1.0, "2026-10-10", "keyword"),
            ):
                self.db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,screened_at)
                  VALUES('doi:10.1/x',?,?,?,1,?,'Reason',?)""", (profile, provider, model, score, stamp))
        records = snapshot_records(self.path, {"timezone": "Asia/Shanghai"})
        paper = json.loads(next(record["data"] for record in records if record["kind"] == "paper"))
        self.assertEqual(paper["score"], .75)
        self.assertEqual(len([record for record in records if record["kind"] == "paper"]), 1)

    def test_snapshot_excludes_paths_credentials_and_raw_source_errors(self):
        secret = "PRIVATE-TOKEN-ONLY-LOCAL"
        private_path = "/Users/private/research/paper.pdf"
        with self.db:
            self.db.execute("""INSERT INTO fulltext_files(identity,stored_path,original_name,sha256,size_bytes,imported_at)
              VALUES('doi:10.1/x',?,'paper.pdf','sha',30,'now')""", (private_path,))
            self.db.execute("""INSERT INTO source_health(source,status,last_error,updated_at)
              VALUES('Journal','failed',?,'now')""", (f"Request failed at {private_path} using {secret}",))
            self.db.execute("""INSERT INTO profile_versions(profile_hash,content,status,created_at)
              VALUES('profile','Current interests','active','now')""")
            self.db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at,relevant_count,details_json)
              VALUES('history','agent-export','succeeded','2026-10-08T01:00:00Z',1,?)""", (canonical({"local_file": private_path, "credential": secret}),))
            self.db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,queue_path,results_path,created_at,imported_at)
              VALUES('history','profile','imported',?,?,'2026-10-08T01:00:00Z','2026-10-08T01:00:00Z')""", (private_path, private_path))
            self.db.execute("""INSERT INTO recommendation_snapshots(run_id,identity,score,reasons,evidence_source)
              VALUES('history','doi:10.1/x',.8,'Historical recommendation','screening')""")
        config = {"state_dir": str(self.root), "api_key": secret, "timezone": "Asia/Shanghai",
                  "cloud_sync": {"credentials_file": private_path, "sync_token": secret},
                  "sources": [{"name": "Journal", "type": "crossref", "secret": secret, "config_path": private_path}]}
        records = snapshot_records(self.path, config)
        payload = canonical(records)
        for omitted in (private_path, secret, str(self.root), "queue_path", "results_path", "stored_path", "credentials_file", "sync_token"):
            self.assertNotIn(omitted, payload)
        self.assertTrue(any(record["kind"] == "history" for record in records))
        for record in records:
            self.assertEqual(record["checksum"], hashlib.sha256(record["data"].encode()).hexdigest())
        self.assertEqual(records, sorted(records, key=lambda record: (record["kind"], record["key"])))
        overview = json.loads(next(record["data"] for record in records if record["kind"] == "meta"))
        self.assertEqual(overview["sources"][0]["health"]["status"], "failed")
        self.assertIn("本地日志", overview["sources"][0]["health"]["last_error"])

    def test_today_snapshot_uses_latest_active_profile_run_not_daily_union(self):
        import datetime as dt
        with self.db:
            self.db.execute("INSERT INTO profile_versions(profile_hash,content,status,created_at) VALUES('active','Interest','active','now')")
            for run, profile, stamp, score in [('earlier','active','2026-10-09T00:00:00Z',.9), ('latest','active','2026-10-09T01:00:00Z',.6), ('different','inactive','2026-10-09T02:00:00Z',.95)]:
                self.db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES(?,'agent-export','succeeded',?)", (run,stamp))
                self.db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,queue_path,created_at,imported_at) VALUES(?,?,'imported','private',?,?)", (run,profile,stamp,stamp))
                self.db.execute("INSERT INTO run_papers(run_id,identity,role) VALUES(?,'doi:10.1/x','selected')", (run,))
                self.db.execute("INSERT INTO screenings(identity,provider,model,profile_hash,score,relevant,reasons,screened_at,run_id) VALUES('doi:10.1/x','codex-agent',?,?,?,1,'Actual judgment',?,?)", (run,profile,score,stamp,run))
                self.db.execute("INSERT INTO recommendation_snapshots(run_id,identity,score,reasons,evidence_source) VALUES(?,'doi:10.1/x',?,'Original daily result','screening')", (run,score))
        with patch("academic_radar.cloud_sync.local_today", return_value=dt.date(2026,10,9)):
            records = snapshot_records(self.path,{"timezone":"Asia/Shanghai"})
        current = json.loads(next(r['data'] for r in records if r['kind']=='history' and r['key']=='current'))
        history = json.loads(next(r['data'] for r in records if r['kind']=='history' and r['key']=='2026-10-09'))
        self.assertEqual(current['batches'],1)
        self.assertEqual(current['papers'],[])
        self.assertEqual(len(history['papers']),1)
        with patch("academic_radar.cloud_sync.local_today", return_value=dt.date(2026,10,10)):
            current = json.loads(next(r['data'] for r in snapshot_records(self.path,{}) if r['kind']=='history' and r['key']=='current'))
        self.assertEqual(current['batches'],0)

    def test_failed_chunk_keeps_active_cloud_snapshot_and_does_not_finish(self):
        records = [dict(kind="paper", key=f"p{i:03}", data="{}", checksum="checksum") for i in range(81)]
        active = {"generation": "original", "records": [{"original": True}]}
        original = json.loads(canonical(active))
        chunks = []
        calls = []
        def fake_request(endpoint, path, credentials, data=None):
            calls.append(path)
            if path.startswith("/api/sync/feedback"):
                return {"events": [], "cursor": 0, "more": False}
            if path == "/api/sync/begin":
                return {"active": False}
            if path == "/api/sync/chunk":
                chunks.append(data["records"])
                if len(chunks) == 2:
                    raise RuntimeError("Network interrupted")
                return {"ok": True}
            if path == "/api/sync/finish":
                active.update(data)
                return data
            raise AssertionError(path)
        with patch("academic_radar.cloud_sync.snapshot_records", return_value=records), patch("academic_radar.cloud_sync.request_json", side_effect=fake_request):
            with self.assertRaisesRegex(RuntimeError, "Network interrupted"):
                _synchronize(self.path, {}, "https://radar.chatgpt.site", {"sync_token": "test"})
        self.assertEqual(active, original)
        self.assertEqual([len(chunk) for chunk in chunks], [40, 40])
        self.assertNotIn("/api/sync/finish", calls)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)

    def test_incremental_snapshot_uploads_only_missing_records(self):
        records = [dict(kind="paper", key="existing", data="{}", checksum="a" * 64),
                   dict(kind="paper", key="changed", data="{}", checksum="b" * 64)]
        uploaded = []
        def request(endpoint, path, credentials, data=None):
            if path.startswith("/api/sync/feedback"):
                return {"events": [], "cursor": 0}
            if path == "/api/sync/begin":
                self.assertEqual(len(data["manifest"]), 2)
                return {"missing": [{"kind": "paper", "key": "changed"}]}
            if path == "/api/sync/chunk":
                uploaded.extend(data["records"])
                return {"accepted": len(data["records"])}
            return {"generation": data["generation"], "count": 2}
        with patch("academic_radar.cloud_sync.snapshot_records", return_value=records), patch("academic_radar.cloud_sync.request_json", side_effect=request):
            result = _synchronize(self.path, {}, "https://radar.chatgpt.site", {"sync_token": "test"})
        self.assertEqual(uploaded, [records[1]])
        self.assertEqual(result["uploaded_records"], 1)

    def test_nonadvancing_feedback_page_stops_instead_of_looping(self):
        with patch("academic_radar.cloud_sync.request_json", return_value={"events": [], "cursor": 0, "more": True}) as request:
            with self.assertRaisesRegex(ValueError, "did not advance"):
                _synchronize(self.path, {}, "https://radar.chatgpt.site", {"sync_token": "test"})
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone())

    def test_origin_validation_rejects_credential_redirection_tricks(self):
        for endpoint in (
            "http://radar.chatgpt.site", "https://evil.example/#radar.chatgpt.site",
            "https://radar.chatgpt.site@evil.example", "https://radar.chatgpt.site.evil.example",
            "https://user:password@radar.chatgpt.site", "https://radar.chatgpt.site/path",
            "https://radar.chatgpt.site?target=evil", "https://radar.chatgpt.site:444",
        ):
            with self.assertRaises(ValueError, msg=endpoint):
                _verified_sites_origin(endpoint)
        self.assertEqual(_verified_sites_origin("https://radar.chatgpt.site/"), "https://radar.chatgpt.site")

    def test_transient_network_failure_retries_without_leaking_credentials(self):
        import io
        from contextlib import contextmanager
        @contextmanager
        def response():
            yield io.BytesIO(b'{"events":[],"cursor":0}')
        with patch("urllib.request.build_opener") as build, patch("academic_radar.cloud_sync.time.sleep") as sleep:
            build.return_value.open.side_effect = [urllib.error.URLError("TLS failed"), response()]
            result = request_json("https://radar.chatgpt.site", "/api/sync/feedback?after=0", {"sync_token":"secret"})
        self.assertEqual(result['cursor'],0)
        self.assertEqual(build.return_value.open.call_count,2)
        sleep.assert_called_once_with(1)

    def test_http_error_does_not_expose_server_body_or_credentials(self):
        secret = "PRIVATE-TOKEN-ONLY-LOCAL"
        error = urllib.error.HTTPError("https://evil.example/" + secret, 307, secret, {"Location": "https://evil.example"}, None)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "HTTP 307") as raised:
                request_json("https://radar.chatgpt.site", "/api/sync/feedback?after=1", {"sync_token": secret})
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("evil.example", str(raised.exception))
        handler = _NoCredentialRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 307, "redirect", {}, "https://evil.example"))


if __name__ == "__main__":
    unittest.main()
