"""Opt-in application-record synchronization; SQLite and credentials stay local."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .recommendations import local_today, recommendation_days, recommendations_on
from .storage import connect, utc_now
from .product import load_config, resolve_state


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def public_source_url(value: str | None) -> str | None:
    """Keep public provenance links without collector contact or API credentials."""
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None
    # Identity is in the API path. Query parameters can carry mailto/API keys.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _stamp(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def apply_remote_feedback(db: sqlite3.Connection, events: list[dict], cursor: int) -> int:
    """Commit a cursor and its events together; newer local edits win conflicts."""
    changed = 0
    if not isinstance(events, list) or type(cursor) is not int or cursor < 0:
        raise ValueError("Invalid cloud feedback page or cursor")
    with db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()
        previous_cursor = int(saved[0]) if saved else 0
        if cursor < previous_cursor:
            raise ValueError("Cloud feedback cursor cannot move backwards")
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("Invalid cloud feedback event")
            if "seq" in event and (type(event["seq"]) is not int or not 0 < event["seq"] <= cursor):
                raise ValueError("Cloud feedback event sequence exceeds its cursor")
            identity = event["identity"]
            if not db.execute("SELECT 1 FROM papers WHERE identity=?", (identity,)).fetchone():
                raise ValueError("Cloud feedback refers to an unknown paper; cursor was not advanced")
            if event.get("interest") not in (None, "interested", "not_interested"):
                raise ValueError("Invalid cloud interest")
            if event.get("reading_status") not in ("unread", "read", "read_later"):
                raise ValueError("Invalid cloud reading status")
            if type(event.get("favorite")) is not int or event["favorite"] not in (0, 1):
                raise ValueError("Invalid cloud favorite")
            reason = str(event.get("reason") or "").strip() or None
            if event.get("interest") and not reason:
                raise ValueError("Cloud preference requires a reason")
            stamp = event["updated_at"]
            if not isinstance(stamp, str):
                raise ValueError("Invalid cloud feedback timestamp")
            remote_stamp = _stamp(stamp)
            previous = db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone()
            if previous and _stamp(previous["updated_at"]) >= remote_stamp:
                continue
            semantic = (previous["interest"] if previous else None) != event["interest"] or (previous["reason"] if previous else None) != reason
            values = (identity, event["interest"], reason, event["favorite"], event["reading_status"], stamp, stamp)
            db.execute("""INSERT INTO paper_feedback(identity,interest,reason,favorite,reading_status,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET interest=excluded.interest,
              reason=excluded.reason,favorite=excluded.favorite,reading_status=excluded.reading_status,updated_at=excluded.updated_at""", values)
            if semantic:
                db.execute("INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES(?,?,?,?,?,?)", values[:6])
                db.execute("UPDATE papers SET needs_rescreen=1 WHERE identity=?", (identity,))
            changed += 1
        db.execute("INSERT INTO meta(key,value) VALUES('cloud_feedback_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cursor),))
    return changed


def snapshot_records(db_path: Path, config: dict) -> list[dict]:
    db = connect(db_path)
    records: list[dict] = []
    def append(kind: str, key: str, value: Any) -> None:
        data = canonical(value)
        records.append({"kind": kind, "key": key, "data": data, "checksum": hashlib.sha256(data.encode()).hexdigest()})
    try:
        db.execute("BEGIN")
        profile = db.execute("SELECT content,profile_hash,confirmed_at FROM profile_versions WHERE status='active'").fetchone()
        profile_hash = profile["profile_hash"] if profile else ""
        scores = """SELECT * FROM (SELECT s.*,ROW_NUMBER() OVER(
          PARTITION BY s.identity ORDER BY s.screened_at DESC,s.rowid DESC) rn
          FROM screenings s WHERE s.provider='codex-agent' AND s.profile_hash=?) WHERE rn=1"""
        query = f"""SELECT p.identity,p.doi,p.title,p.abstract,p.venue,p.published,p.published_precision,p.url,p.authors_json,
          p.abstract_source,p.abstract_source_url,p.abstract_retrieved_at,p.eligibility_status,p.publication_type,
          p.needs_rescreen,s.score,s.reasons,s.confidence,s.themes_json,s.reasoning_json,s.score_dimensions_json,s.rubric_version
          FROM papers p LEFT JOIN ({scores}) s ON s.identity=p.identity AND s.rn=1 ORDER BY p.identity"""
        papers = [dict(row) for row in db.execute(query, (profile_hash,))]
        for paper in papers:
            paper["abstract_source_url"] = public_source_url(paper["abstract_source_url"])
            append("paper", paper["identity"], paper)
        timezone = config.get("timezone", "Asia/Shanghai")
        days = recommendation_days(db, timezone)
        for day in days:
            history, batches, incomplete = recommendations_on(db, dt.date.fromisoformat(day), timezone)
            allowed = {"identity", "score", "reasons", "confidence", "themes_json", "reasoning_json", "score_dimensions_json", "rubric_version", "evidence_source"}
            append("history", day, {"papers": [{k: p[k] for k in allowed if k in p} for p in history], "batches": batches, "incomplete": incomplete})
        # Today follows the same latest completed active-profile run as the
        # local web app. Historical days retain their immutable daily union.
        latest = db.execute("""SELECT run_id,imported_at FROM agent_jobs WHERE status='imported' AND profile_hash=?
          ORDER BY imported_at DESC LIMIT 1""", (profile_hash,)).fetchone()
        current: list[dict] = []
        day = local_today(timezone)
        is_today = bool(latest and _stamp(latest["imported_at"]).astimezone(ZoneInfo(timezone)).date() == day)
        if is_today:
            current = [dict(row) for row in db.execute("""SELECT p.identity,s.score,s.reasons,s.confidence,
              s.themes_json,s.reasoning_json,s.score_dimensions_json,s.rubric_version FROM run_papers rp
              JOIN papers p ON p.identity=rp.identity JOIN screenings s ON s.identity=p.identity AND s.run_id=rp.run_id
              WHERE rp.run_id=? AND s.provider='codex-agent' AND s.profile_hash=? AND p.eligibility_status='eligible'
              AND s.score>=? AND (rp.role='selected' OR (rp.role='selected_new' AND NOT EXISTS(
                SELECT 1 FROM run_papers chosen WHERE chosen.run_id=rp.run_id AND chosen.identity=rp.identity AND chosen.role='selected')))
              ORDER BY p.identity""", (latest["run_id"], profile_hash, config.get("relevance_threshold", .70)))]
        append("history", "current", {"day": day.isoformat(), "papers": current, "batches": int(is_today), "incomplete": False})
        for feedback in db.execute("SELECT * FROM paper_feedback ORDER BY identity"):
            append("feedback", feedback["identity"], dict(feedback))
        sources = []
        for source in config.get("sources", []):
            item = {key: source[key] for key in ("name", "type", "issn", "official_issues_url") if key in source}
            health = db.execute("SELECT status,last_success_at,last_error,updated_at FROM source_health WHERE source=?", (source["name"],)).fetchone()
            item["health"] = dict(health) if health else {"status": "unknown"}
            if item["health"].get("last_error"):
                item["health"]["last_error"] = "最近采集失败，详细信息保存在本地日志"
            refresh = db.execute("""SELECT checked_at,detail FROM official_issue_checks WHERE source_name=?
              AND status='succeeded' AND issue_key LIKE 'metadata-latest-two-as-of-%'
              ORDER BY checked_at DESC LIMIT 1""", (source["name"],)).fetchone()
            if refresh:
                try:
                    evidence = json.loads(refresh["detail"])
                    if evidence.get("phase") == "latest_two_metadata_refresh":
                        item["issue_check"] = {"checked_at": refresh["checked_at"], "as_of_date": evidence["as_of_date"],
                            "mode": "publisher_deposited_metadata", "issue_keys": evidence["issue_keys"],
                            "uncertain_date": any(i.get("uncertain_date_count", 0) for i in evidence.get("issues", []))}
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
            sources.append(item)
        append("private", "profile", dict(profile) if profile else {})
        totals = dict(db.execute("SELECT COUNT(*) total,SUM(eligibility_status='eligible') eligible,SUM(COALESCE(abstract,'')='') missing_abstracts FROM papers").fetchone())
        latest_import = db.execute("SELECT MAX(imported_at) t FROM agent_jobs WHERE status='imported' AND profile_hash=?", (profile_hash,)).fetchone()[0]
        append("meta", "overview", {"sources": sources, "counts": totals, "days": days, "timezone": timezone,
                                   "threshold": config.get("relevance_threshold", 0.70), "last_import": latest_import})
        return sorted(records, key=lambda record: (record["kind"], record["key"]))
    finally:
        db.close()


def _verified_sites_origin(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".chatgpt.site")
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Cloud synchronization requires a verified HTTPS Sites origin")
    return "https://" + parsed.hostname


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward bearer credentials to a redirect destination.
        return None


def request_json(endpoint: str, path: str, credentials: dict, data: dict | None = None) -> dict:
    endpoint = _verified_sites_origin(endpoint)
    headers = {"Authorization": "Bearer " + credentials["sync_token"], "Accept": "application/json"}
    if credentials.get("sites_token"):
        headers["OAI-Sites-Authorization"] = "Bearer " + credentials["sites_token"]
    body = None
    if data is not None:
        body = canonical(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint.rstrip("/") + path, data=body, headers=headers)
    opener = urllib.request.build_opener(_NoCredentialRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=45) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if attempt < 2 and error.code in (408, 429, 500, 502, 503, 504):
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                if 0 <= delay <= 20:
                    time.sleep(delay)
                    continue
            # Never log headers, credential material, redirects or server bodies.
            raise RuntimeError(f"Cloud sync request rejected (HTTP {error.code}, path {path.split('?')[0]})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("Cloud sync connection unavailable; the previous complete snapshot remains active") from None
            time.sleep(2 ** attempt)
    raise RuntimeError("Cloud sync connection unavailable")


def sync_configured(config_path: Path) -> dict:
    config = load_config(config_path)
    settings = config.get("cloud_sync", {})
    if not settings.get("enabled"):
        return {"status": "disabled"}
    endpoint = _verified_sites_origin(str(settings.get("endpoint", "")))
    state = resolve_state(config_path, config)
    credentials_path = Path(settings.get("credentials_file", str(state / "cloud-sync.json"))).expanduser()
    if not credentials_path.is_absolute():
        credentials_path = config_path.expanduser().resolve().parent / credentials_path
    credentials = json.loads(credentials_path.read_text())
    lock_path = state / ".cloud-sync.lock"
    # Do not race the daily import, the periodic service or a manual retry.
    with lock_path.open("a") as lock:
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        try:
            result = _synchronize(state / "papers.sqlite3", config, endpoint, credentials)
            result["synced_at"] = utc_now()
            _save_status(state, result)
            return result
        except Exception as error:
            _save_status(state, {"status": "failed", "checked_at": utc_now(), "error": type(error).__name__ + ": " + str(error)[:300]})
            raise


def _save_status(state: Path, result: dict) -> None:
    path = state / "cloud-sync-status.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(canonical(result), encoding="utf-8")
    os.replace(temp, path)


def _synchronize(db_path: Path, config: dict, endpoint: str, credentials: dict) -> dict:
    db = connect(db_path)
    pulled = 0
    try:
        row = db.execute("SELECT value FROM meta WHERE key='cloud_feedback_cursor'").fetchone()
        cursor = int(row[0]) if row else 0
        while True:
            response = request_json(endpoint, f"/api/sync/feedback?after={cursor}", credentials)
            if response.get("more") and (type(response.get("cursor")) is not int or response["cursor"] <= cursor):
                raise ValueError("Cloud feedback pagination did not advance its cursor")
            pulled += apply_remote_feedback(db, response["events"], response["cursor"])
            cursor = response["cursor"]
            if not response.get("more"):
                break
    finally:
        db.close()
    records = snapshot_records(db_path, config)
    fingerprint = "\n".join(f"{r['kind']}:{r['key']}:{r['checksum']}" for r in records)
    generation = hashlib.sha256(fingerprint.encode()).hexdigest()
    manifest = [{k: row[k] for k in ("kind", "key", "checksum")} for row in records]
    opened = request_json(endpoint, "/api/sync/begin", credentials, {"generation": generation, "count": len(records), "manifest": manifest})
    if opened.get("active"):
        return {"status": "unchanged", "generation": generation, "records": len(records), "feedback_imported": pulled}
    pending = records
    if "missing" in opened:
        missing = {(row["kind"], row["key"]) for row in opened["missing"]}
        pending = [row for row in records if (row["kind"], row["key"]) in missing]
    chunk: list[dict] = []
    size = 0
    for row in pending:
        row_size = len(canonical(row).encode())
        if row_size > 1500000:
            raise ValueError("Cloud snapshot record exceeds the safe upload size")
        if chunk and (len(chunk) == 40 or size + row_size > 1500000):
            request_json(endpoint, "/api/sync/chunk", credentials, {"generation": generation, "records": chunk})
            chunk, size = [], 0
        chunk.append(row)
        size += row_size
    if chunk:
        request_json(endpoint, "/api/sync/chunk", credentials, {"generation": generation, "records": chunk})
    completed = request_json(endpoint, "/api/sync/finish", credentials, {"generation": generation})
    if completed.get("generation") != generation or completed.get("count") != len(records):
        raise ValueError("Cloud snapshot verification failed; remote activation could not be confirmed")
    return {"status": "succeeded", "generation": generation, "records": len(records), "uploaded_records": len(pending), "feedback_imported": pulled}
