import datetime as dt
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from academic_radar.engagement import seed_active_profile
from academic_radar.recommendations import (
    SCREENING_RUBRIC_VERSION, calibrated_score, evaluation_policy,
    recommendation_days, recommendations_on, snapshot_run,
    unit_number, validate_judgment_evidence, validate_study_summary,
)
from academic_radar.storage import connect, load_migrations, upgrade_database
from academic_radar.web import create_app


class RecommendationQualityTests(unittest.TestCase):
    def test_research_narrative_requires_three_distinct_stages_in_order(self):
        summary = {
            "problem_motivation": "研究关注偏好输入变化后调度决策能否稳定回应，这是可控策略的关键问题。",
            "approach": "作者把偏好向量输入调度策略，利用仿真实验检查决策与输入偏好的匹配。",
            "findings_value": "仿真中偏好匹配改善，可借鉴其校准指标；原文未进行用户实验，尚不能推断协作收益。",
        }
        item = {"study_summary": summary, "recommendation_reason": "\n\n".join(summary.values())}
        self.assertEqual(validate_study_summary(item), summary)
        item["recommendation_reason"] = "\n\n".join(reversed(list(summary.values())))
        with self.assertRaisesRegex(ValueError, "order"):
            validate_study_summary(item)
        item["study_summary"] = {"problem_motivation": "问题", "approach": "方法"}
        with self.assertRaisesRegex(ValueError, "requires"):
            validate_study_summary(item)
        self.assertIsNone(validate_study_summary({"recommendation_reason": "Existing exported judgment"}))

    def setUp(self):
        self.paper = {
            "title": "Preference-conditioned dispatching",
            "abstract": (
                "The policy receives a preference vector and calibrates its dispatch decisions. "
                "Simulation improves preference matching, but no user study was conducted."
            ),
        }
        self.item = {
            "recommendation_type": "core",
            "matched_themes": ["preference integration"],
            "reasoning": {
                "evidence_summary": "算法在仿真中接收偏好向量并校准调度决策，但没有进行用户实验。",
                "profile_connection": "偏好直接进入搜索策略，与用户引导优化的机制一致。",
                "transfer_value": "可测试偏好权重变化后路线策略的响应稳定性。",
                "limitations": "仿真性能不能证明人的表达成本或人机联合收益。",
            },
            "evidence_anchors": [{
                "source": "abstract",
                "quote": "The policy receives a preference vector and calibrates its dispatch decisions.",
                "claim": "原文支持偏好进入策略及输出校准这一机制，但不支持用户收益结论。",
            }],
        }
        self.dimensions = {
            "core_relevance": 1.0, "mechanism_alignment": 1.0,
            "method_transfer": 1.0, "evidence_quality": 1.0,
            "boundary_penalty": 0.0,
        }

    def test_anchors_preserve_exact_source_and_separate_proposed_transfer(self):
        quality = validate_judgment_evidence(self.item, self.paper)
        self.assertEqual(quality["recommendation_type"], "core")
        self.assertEqual(quality["evidence_anchors"], self.item["evidence_anchors"])
        self.assertNotIn("score", quality)

    def test_invented_result_is_rejected_even_when_relevance_dimensions_are_high(self):
        self.item["evidence_anchors"][0]["quote"] = "A user study of 48 people improved joint decision quality."
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_judgment_evidence(self.item, self.paper)

    def test_formatted_publisher_excerpt_matches_without_paraphrase(self):
        self.paper["abstract"] = "<jats:p>The policy receives a preference\u00a0vector &amp; calibrates dispatch decisions.</jats:p>"
        self.item["evidence_anchors"][0]["quote"] = "The policy receives a preference vector & calibrates dispatch decisions."
        self.assertEqual(len(validate_judgment_evidence(self.item, self.paper)["evidence_anchors"]), 1)
        self.item["evidence_anchors"][0]["quote"] = "The policy receives user feedback & calibrates dispatch decisions."
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_judgment_evidence(self.item, self.paper)

    def test_title_only_cannot_ignore_an_available_abstract(self):
        self.item["evidence_anchors"][0].update(source="title", quote=self.paper["title"])
        with self.assertRaisesRegex(ValueError, "available abstract"):
            validate_judgment_evidence(self.item, self.paper)
        self.paper["abstract"] = ""
        self.assertEqual(validate_judgment_evidence(self.item, self.paper)["evidence_anchors"][0]["source"], "title")
        self.assertLess(calibrated_score(self.dimensions, "core", abstract_missing=True), .70)

    def test_duplicate_trivial_and_unattributed_anchors_are_rejected(self):
        for mutation, message in (
            (lambda item: item["evidence_anchors"].append(copy.deepcopy(item["evidence_anchors"][0])), "duplicate"),
            (lambda item: item["evidence_anchors"][0].update(quote="policy"), "too short"),
            (lambda item: item["evidence_anchors"][0].update(source="model-summary"), "source"),
            (lambda item: item["evidence_anchors"][0].update(claim="有价值"), "substantive"),
            (lambda item: item.update(evidence_anchors=[]), "1 to 3"),
        ):
            with self.subTest(message=message):
                item = copy.deepcopy(self.item)
                mutation(item)
                with self.assertRaisesRegex(ValueError, message):
                    validate_judgment_evidence(item, self.paper)

    def test_core_and_method_transfer_require_a_named_profile_theme(self):
        for kind in ("core", "method_transfer"):
            for themes in ([], [" "], None, "preference"):
                with self.subTest(kind=kind, themes=themes):
                    self.item.update(recommendation_type=kind, matched_themes=themes)
                    with self.assertRaisesRegex(ValueError, "named matched theme"):
                        validate_judgment_evidence(self.item, self.paper)
        self.item.update(recommendation_type="outside", matched_themes=[])
        self.assertEqual(validate_judgment_evidence(self.item, self.paper)["recommendation_type"], "outside")

    def test_copying_one_audit_sentence_into_every_field_is_rejected(self):
        self.item["reasoning"] = dict.fromkeys(self.item["reasoning"], "论文与当前研究主题有联系，具备一定的参考价值和迁移潜力。")
        with self.assertRaisesRegex(ValueError, "distinct evidence"):
            validate_judgment_evidence(self.item, self.paper)

    def test_relationship_class_prevents_adjacent_or_method_only_score_inflation(self):
        expected = {"core": 1.0, "method_transfer": .84, "adjacent": .69, "outside": .29}
        for kind, cap in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(calibrated_score(self.dimensions, kind), cap)
        self.dimensions.update(core_relevance=.9, mechanism_alignment=.8,
                               method_transfer=.7, evidence_quality=.6, boundary_penalty=.2)
        self.assertEqual(calibrated_score(self.dimensions, "core"), .72)
        self.assertEqual(calibrated_score(self.dimensions, "method_transfer"), .72)
        self.assertEqual(calibrated_score(self.dimensions, "adjacent"), .69)

    def test_numbers_reject_non_finite_overflow_boolean_strings_and_out_of_range(self):
        for value in (float("nan"), float("inf"), -float("inf"), 10**1000,
                      True, False, "0.9", None, -.01, 1.01):
            with self.subTest(value=repr(value)[:32]):
                with self.assertRaises(ValueError):
                    unit_number(value, "confidence")
                self.dimensions["evidence_quality"] = value
                with self.assertRaises(ValueError):
                    calibrated_score(self.dimensions, "core")
        self.assertEqual(unit_number(0, "confidence"), 0.0)
        self.assertEqual(unit_number(1, "confidence"), 1.0)

    def test_queue_policy_is_fresh_and_carries_executable_and_semantic_rules(self):
        first = evaluation_policy()
        self.assertEqual(first["rubric_version"], SCREENING_RUBRIC_VERSION)
        self.assertIn("evidence_anchors", first["result_fields"])
        self.assertIn("recommendation_type", first["result_fields"])
        self.assertEqual(first["score_caps"]["method_transfer"], .84)
        self.assertTrue(any("non-significant" in rule for rule in first["requirements"]))
        first["score_caps"]["method_transfer"] = 1
        first["minimum_reasoning_characters"]["limitations"] = 1
        second = evaluation_policy()
        self.assertEqual(second["score_caps"]["method_transfer"], .84)
        self.assertEqual(second["minimum_reasoning_characters"]["limitations"], 18)


class RecommendationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'papers.sqlite3'
        self.config = self.root / 'config.toml'
        self.config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\ntimezone = "Asia/Shanghai"\n')
        (self.root / 'research-profile.md').write_text('profile')
        # Exercise the real upgrade path with historical data already present.
        migrations = load_migrations()
        with patch('academic_radar.storage.load_migrations', return_value=[m for m in migrations if m.version < 14]):
            upgrade_database(self.path)
            self.profile = seed_active_profile(self.path, 'profile')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        with self.db:
            self.db.execute("""INSERT INTO papers(identity,title,abstract,first_seen,updated_at,eligibility_status)
              VALUES('p','Historical paper','Abstract','2020','2020','eligible')""")

    def run_record(self, run_id, stamp, score=.9, status='imported', selected=True):
        with self.db:
            self.db.execute("INSERT INTO pipeline_runs(run_id,kind,status,started_at) VALUES(?,'agent-export','succeeded',?)", (run_id, stamp))
            self.db.execute("INSERT INTO agent_jobs(run_id,profile_hash,status,created_at,imported_at) VALUES(?,?,?,?,?)",
                            (run_id, self.profile['profile_hash'], status, stamp, stamp))
            self.db.execute("""INSERT OR REPLACE INTO screenings(identity,profile_hash,provider,model,relevant,score,reasons,
              themes_json,confidence,screened_at,run_id) VALUES('p',?,'codex-agent','test',1,?,?,'[]',.9,?,?)""",
                            (self.profile['profile_hash'], score, 'Reason ' + run_id, stamp, run_id))
            if selected:
                self.db.execute("INSERT INTO run_papers VALUES(?,'p','selected')", (run_id,))
                self.db.execute("INSERT INTO run_papers VALUES(?,'p','selected_new')", (run_id,))

    def upgrade(self):
        upgrade_database(self.path)

    def client(self):
        return TestClient(create_app(self.config))

    def test_migration_keeps_membership_when_old_score_was_overwritten(self):
        self.run_record('old', '2026-09-12T02:00:00Z')
        self.run_record('new', '2026-09-13T02:00:00Z')
        self.upgrade()
        old, count, _ = recommendations_on(self.db, dt.date(2026, 9, 12), 'Asia/Shanghai')
        self.assertEqual((len(old), count), (1, 1))
        self.assertIsNone(old[0]['score'])
        self.assertEqual(old[0]['evidence_source'], 'unavailable')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recommendation_snapshots').fetchone()[0], 2)
        self.assertEqual(upgrade_database(self.path)['applied'], [])

    def test_snapshots_survive_rescreen_profile_change_and_deduplicate_same_day(self):
        self.upgrade()
        self.run_record('first', '2026-09-12T17:00:00Z', .85)
        with self.db:
            snapshot_run(self.db, 'first')
        self.run_record('second', '2026-09-13T12:00:00Z', .95)
        with self.db:
            snapshot_run(self.db, 'second')
            self.db.execute("UPDATE screenings SET score=.1,reasons='New unrelated judgment'")
            self.db.execute("UPDATE profile_versions SET status='superseded'")
            self.db.execute("UPDATE papers SET eligibility_status='excluded'")
        papers, count, _ = recommendations_on(self.db, dt.date(2026, 9, 13), 'Asia/Shanghai')
        self.assertEqual((len(papers), count), (1, 2))
        self.assertEqual((papers[0]['score'], papers[0]['reasons']), (.95, 'Reason second'))

    def test_date_boundaries_use_platform_timezone_and_completed_imports(self):
        self.upgrade()
        for run_id, stamp, status in (
            ('before', '2026-09-12T15:59:59Z', 'imported'),
            ('start', '2026-09-12T16:00:00Z', 'imported'),
            ('end', '2026-09-13T16:00:00Z', 'imported'),
            ('pending', '2026-09-13T09:00:00Z', 'exported'),
        ):
            self.run_record(run_id, stamp, status=status)
            with self.db:
                snapshot_run(self.db, run_id)
        papers, count, _ = recommendations_on(self.db, dt.date(2026, 9, 13), 'Asia/Shanghai')
        self.assertEqual((count, papers[0]['reasons']), (1, 'Reason start'))
        self.assertEqual(recommendation_days(self.db, 'Asia/Shanghai'), ['2026-09-14', '2026-09-13', '2026-09-12'])

    def test_yesterday_default_and_older_filter_preserve_return_link(self):
        self.run_record('yesterday', '2026-09-13T02:00:00Z')
        self.upgrade()
        with patch('academic_radar.web.local_today', return_value=dt.date(2026, 9, 14)), self.client() as client:
            page = client.get('/').text
            self.assertIn('昨日推荐', page)
            self.assertIn('data-recommendation-date="2026-09-13"', page)
            self.assertIn('Reason yesterday', page)
            self.assertIn('/?history_date=2026-09-13#history-paper-p', page)
            older = client.get('/?history_date=2026-09-12').text
            self.assertIn('当天没有完成推荐更新', older)
            self.assertNotIn('Historical paper', older)
            for value in ('not-a-date', '2026-02-30', '2026-09-14', '2026-09-15'):
                self.assertEqual(client.get('/?history_date=' + value).status_code, 400)

    def test_empty_completed_day_is_different_from_missing_update(self):
        self.run_record('empty', '2026-09-13T02:00:00Z', selected=False)
        self.upgrade()
        with patch('academic_radar.web.local_today', return_value=dt.date(2026, 9, 14)), self.client() as client:
            self.assertIn('当天没有入选论文', client.get('/').text)
            self.assertIn('当天没有完成推荐更新', client.get('/?history_date=2026-09-12').text)
            with self.db:
                self.db.execute("UPDATE pipeline_runs SET relevant_count=3 WHERE run_id='empty'")
            page = client.get('/').text
            self.assertIn('历史推荐详情暂不可用', page)
            self.assertNotIn('当天没有入选论文', page)

    def test_today_and_history_can_render_same_paper_with_unique_anchors(self):
        today = dt.datetime.now(dt.timezone.utc)
        yesterday = today - dt.timedelta(days=1)
        self.run_record('old', yesterday.isoformat())
        self.upgrade()
        self.run_record('new', today.isoformat())
        with self.db:
            snapshot_run(self.db, 'new')
        with self.client() as client:
            page = client.get('/').text
        self.assertEqual(page.count('id="paper-p"'), 1)
        self.assertEqual(page.count('id="history-paper-p"'), 1)
        self.assertEqual(page.count('data-paper-identity="p"'), 2)
