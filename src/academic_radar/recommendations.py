"""Immutable recommendation judgments and timezone-aware daily retrieval."""

from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo


def local_today(timezone: str) -> dt.date:
    return dt.datetime.now(ZoneInfo(timezone)).date()


def recommendation_days(db: sqlite3.Connection, timezone: str) -> list[str]:
    days = set()
    for job in db.execute("SELECT imported_at FROM agent_jobs WHERE status='imported'"):
        try:
            stamp = dt.datetime.fromisoformat((job[0] or '').replace('Z', '+00:00'))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        days.add(stamp.astimezone(ZoneInfo(timezone)).date().isoformat())
    return sorted(days, reverse=True)


def snapshot_run(db: sqlite3.Connection, run_id: str) -> None:
    """Call inside the import transaction, before a later run can replace scores."""
    db.execute("""INSERT OR IGNORE INTO recommendation_snapshots(
      run_id,identity,score,reasons,confidence,themes_json,screened_at,
      reasoning_json,score_dimensions_json,rubric_version,evidence_source)
      SELECT DISTINCT s.run_id,s.identity,s.score,s.reasons,s.confidence,s.themes_json,
        s.screened_at,s.reasoning_json,s.score_dimensions_json,s.rubric_version,'screening'
      FROM screenings s JOIN agent_jobs aj ON aj.run_id=s.run_id AND aj.profile_hash=s.profile_hash
      WHERE s.run_id=? AND s.provider='codex-agent' AND EXISTS(
        SELECT 1 FROM run_papers rp WHERE rp.run_id=s.run_id AND rp.identity=s.identity
          AND rp.role IN ('selected','selected_new'))
      ORDER BY s.screened_at DESC,s.rowid DESC""", (run_id,))


def recommendations_on(db: sqlite3.Connection, day: dt.date, timezone: str) -> tuple[list[dict], int, bool]:
    zone = ZoneInfo(timezone)
    start = dt.datetime.combine(day, dt.time.min, zone).isoformat()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, zone).isoformat()
    jobs = db.execute("""SELECT aj.run_id,pr.relevant_count,
      (SELECT COUNT(*) FROM recommendation_snapshots s WHERE s.run_id=aj.run_id) saved_count
      FROM agent_jobs aj LEFT JOIN pipeline_runs pr ON pr.run_id=aj.run_id WHERE aj.status='imported'
      AND julianday(aj.imported_at)>=julianday(?) AND julianday(aj.imported_at)<julianday(?)""",
      (start, end)).fetchall()
    if not jobs:
        return [], 0, False
    incomplete = any((job['relevant_count'] or 0) > job['saved_count'] for job in jobs)
    # Do not apply today's profile or threshold to an actual historical selection.
    # For multiple imports on one day keep each selected paper once, with its
    # latest selected judgment from that day. Feedback remains current everywhere.
    result = db.execute("""WITH ranked AS (
      SELECT s.*,aj.imported_at,ROW_NUMBER() OVER(
        PARTITION BY s.identity ORDER BY julianday(aj.imported_at) DESC,s.run_id DESC) n
      FROM recommendation_snapshots s JOIN agent_jobs aj ON aj.run_id=s.run_id
      WHERE aj.status='imported' AND julianday(aj.imported_at)>=julianday(?)
        AND julianday(aj.imported_at)<julianday(?)
    ) SELECT p.*,s.score,s.reasons,s.confidence,s.themes_json,s.screened_at,
      s.reasoning_json,s.score_dimensions_json,s.rubric_version,s.evidence_source,
      f.interest,f.reason feedback_reason,COALESCE(f.favorite,0) favorite,
      COALESCE(f.reading_status,'unread') reading_status,
      (SELECT COUNT(*) FROM fulltext_files ft WHERE ft.identity=p.identity) fulltext_count,
      (SELECT ft.id FROM fulltext_files ft WHERE ft.identity=p.identity
        ORDER BY ft.imported_at DESC,ft.id DESC LIMIT 1) fulltext_id
      FROM ranked s JOIN papers p ON p.identity=s.identity
      LEFT JOIN paper_feedback f ON f.identity=p.identity WHERE s.n=1
      ORDER BY CASE WHEN f.interest IS NULL THEN 0 ELSE 1 END,
        CASE f.interest WHEN 'not_interested' THEN 1 ELSE 0 END,s.score DESC,p.published DESC,p.identity""",
      (start, end))
    return [dict(item) for item in result], len(jobs), incomplete
