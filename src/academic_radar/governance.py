"""Publication-type governance and recoverable cleanup audits."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .storage import backup_database, connect, utc_now


ALLOWED_TYPES = {"Journal Article", "Conference Paper"}

RAW_TYPE_MAP = {
    "journal-article": ("Journal Article", "eligible", "期刊论文"),
    "research-article": ("Journal Article", "eligible", "期刊论文"),
    "original-article": ("Journal Article", "eligible", "期刊论文"),
    "review-article": ("Journal Article", "eligible", "期刊论文"),
    "proceedings-article": ("Conference Paper", "eligible", "会议论文"),
    "conference-paper": ("Conference Paper", "eligible", "会议论文"),
    "conference": ("Conference Paper", "eligible", "会议论文"),
    "editorial": ("Editorial", "excluded", "编辑性内容"),
    "paratext": ("Front/Back Matter", "excluded", "前置或后置材料"),
    "correction": ("Correction", "excluded", "更正或勘误"),
    "corrigendum": ("Correction", "excluded", "更正或勘误"),
    "erratum": ("Correction", "excluded", "更正或勘误"),
    "letter": ("Letter", "excluded", "来信或短评"),
    "correspondence": ("Letter", "excluded", "来信或短评"),
    "comment": ("Comment", "excluded", "评论性内容"),
    "commentary": ("Comment", "excluded", "评论性内容"),
    "news-and-views": ("News", "excluded", "新闻或评论性内容"),
    "news-views": ("News", "excluded", "新闻或评论性内容"),
    "research-briefing": ("Comment", "excluded", "研究简报或评论性内容"),
    "news": ("News", "excluded", "新闻或公告"),
    "book-review": ("Book Review", "excluded", "书评"),
    "posted-content": ("Other", "excluded", "非正式发表内容"),
    "reference-entry": ("Other", "excluded", "参考条目"),
    "journal-issue": ("Front/Back Matter", "excluded", "整期期刊材料"),
    "journal-volume": ("Front/Back Matter", "excluded", "整卷期刊材料"),
    "proceedings": ("Front/Back Matter", "excluded", "整本会议录"),
}

NEGATIVE_TITLE_RULES = [
    (r"(?:^|[;:\-–—]\s*)\s*(editorial\s+board|editorial|editor['’]s?\s+note)\b", "Editorial", "编辑性内容"),
    (r"\bextended\s+abstracts?\b", "Extended Abstract", "扩展摘要"),
    (r"^\s*(corrigendum|correction|erratum)\b", "Correction", "更正或勘误"),
    (r"^\s*(letter\s+to\s+the\s+editor|comment\s+on)\b", "Letter", "来信或评论"),
    (r"\b(?:a\s+)?commentary\s+on\b", "Comment", "评论性内容"),
    (r"^\s*(news|announcement)\b", "News", "新闻或公告"),
    (r"\bbook\s+review\b", "Book Review", "书评"),
    (r"\bcall\s+for\s+papers\b", "Call for Papers", "征稿通知"),
    (r"^\s*(front\s+matter|back\s+matter)\b", "Front/Back Matter", "前置或后置材料"),
]


def publication_decision(
    title: str,
    venue: str = "",
    raw_type: str = "",
    source_name: str = "",
    source_kind: str = "",
) -> dict[str, Any]:
    """Return a conservative, evidence-bearing publication decision."""

    raw = re.sub(r"[^a-z0-9]+", "-", (raw_type or "").strip().lower()).strip("-")
    evidence: list[dict[str, str]] = []
    if raw:
        evidence.append({"kind": "metadata_type", "source": source_name or "metadata", "value": raw_type})

    negative = None
    combined = f"{title} {venue}".strip()
    for pattern, normalized, reason in NEGATIVE_TITLE_RULES:
        if re.search(pattern, combined, flags=re.IGNORECASE):
            negative = (normalized, reason, pattern)
            evidence.append({"kind": "title_rule", "source": "local-rule", "value": pattern})
            break

    mapped = RAW_TYPE_MAP.get(raw)
    if negative:
        normalized, reason, _ = negative
        return {
            "publication_type": normalized,
            "eligibility_status": "excluded",
            "exclusion_reason": reason,
            "evidence": evidence,
        }
    if mapped:
        normalized, status, reason = mapped
        return {
            "publication_type": normalized,
            "eligibility_status": status,
            "exclusion_reason": None if status == "eligible" else reason,
            "evidence": evidence,
        }

    # OpenAlex historically used the broad value "article". It is only a
    # positive signal when the hosting source type independently identifies a
    # journal or conference.
    if raw == "article" and source_kind.lower() in {"journal", "journals"}:
        evidence.append({"kind": "source_type", "source": source_name or "metadata", "value": source_kind})
        return {
            "publication_type": "Journal Article",
            "eligibility_status": "eligible",
            "exclusion_reason": None,
            "evidence": evidence,
        }
    if raw == "article" and source_kind.lower() in {"conference", "proceedings"}:
        evidence.append({"kind": "source_type", "source": source_name or "metadata", "value": source_kind})
        return {
            "publication_type": "Conference Paper",
            "eligibility_status": "eligible",
            "exclusion_reason": None,
            "evidence": evidence,
        }

    return {
        "publication_type": "Unknown",
        "eligibility_status": "quarantine",
        "exclusion_reason": "出版类型证据不足，等待核查",
        "evidence": evidence,
    }


def latest_scores_sql() -> str:
    return """SELECT * FROM (
      SELECT s.*,ROW_NUMBER() OVER(PARTITION BY s.identity ORDER BY s.screened_at DESC,s.rowid DESC) rn
      FROM screenings s WHERE s.provider='codex-agent'
    ) WHERE rn=1"""


def source_kind_from_evidence(value: str) -> str:
    """Recover the independently observed host kind for repeatable audits."""

    try:
        evidence = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(evidence, list):
        return ""
    for item in evidence:
        if isinstance(item, dict) and item.get("kind") == "source_type":
            return str(item.get("value") or "")
    return ""


def governance_stats(db_path: Path, threshold: float = 0.70) -> dict[str, Any]:
    db = connect(db_path)
    try:
        total = int(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        status_counts = {
            row["eligibility_status"]: int(row["count"])
            for row in db.execute(
                "SELECT eligibility_status,COUNT(*) count FROM papers GROUP BY eligibility_status"
            )
        }
        reasons = {
            row["reason"]: int(row["count"])
            for row in db.execute(
                """SELECT COALESCE(exclusion_reason,'未分类') reason,COUNT(*) count
                FROM papers WHERE eligibility_status<>'eligible'
                GROUP BY COALESCE(exclusion_reason,'未分类') ORDER BY count DESC"""
            )
        }
        latest = latest_scores_sql()
        below = int(
            db.execute(
                f"""SELECT COUNT(*) FROM papers p JOIN ({latest}) s ON s.identity=p.identity
                WHERE p.eligibility_status='eligible' AND s.score<?""",
                (threshold,),
            ).fetchone()[0]
        )
        eligible = int(status_counts.get("eligible", 0))
        visible = int(
            db.execute(
                f"""SELECT COUNT(*) FROM papers p JOIN ({latest}) s ON s.identity=p.identity
                WHERE p.eligibility_status='eligible' AND s.score>=?""",
                (threshold,),
            ).fetchone()[0]
        )
        abstract_row = db.execute(
            f"""SELECT COUNT(*) total,SUM(CASE WHEN COALESCE(p.abstract,'')<>'' THEN 1 ELSE 0 END) abstracts
            FROM papers p JOIN ({latest}) s ON s.identity=p.identity
            WHERE p.eligibility_status='eligible' AND s.score>=?""",
            (threshold,),
        ).fetchone()
        abstract_total = int(abstract_row["total"] or 0)
        abstract_count = int(abstract_row["abstracts"] or 0)
        return {
            "total": total,
            "eligible": eligible,
            "excluded": int(status_counts.get("excluded", 0)),
            "quarantine": int(status_counts.get("quarantine", 0)),
            "below_threshold": below,
            "visible": visible,
            "exclusion_reasons": reasons,
            "abstracts": abstract_count,
            "missing_abstracts": abstract_total - abstract_count,
            "abstract_percent": round(abstract_count / abstract_total * 100, 1) if abstract_total else 0.0,
        }
    finally:
        db.close()


def preview_cleanup(
    db_path: Path,
    state_dir: Path,
    threshold: float,
    backup_path: Path | None = None,
) -> dict[str, Any]:
    """Create a non-mutating cleanup preview tied to a verified online backup."""

    state = state_dir.expanduser().resolve()
    audit_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup = backup_path or state / "backups" / f"cleanup-{audit_id}.sqlite3"
    if not backup.exists():
        backup_database(db_path, backup)
    before = governance_stats(db_path, threshold)
    db = connect(db_path)
    try:
        items = []
        reason_counts: Counter[str] = Counter()
        for paper in db.execute(
            """SELECT identity,doi,title,venue,publication_type_raw,publication_type_source,
            publication_type_evidence_json FROM papers ORDER BY identity"""
        ):
            decision = publication_decision(
                paper["title"], paper["venue"] or "", paper["publication_type_raw"] or "",
                paper["publication_type_source"] or "",
                source_kind_from_evidence(paper["publication_type_evidence_json"]),
            )
            if decision["eligibility_status"] != "eligible":
                reason_counts[decision["exclusion_reason"] or "未分类"] += 1
            items.append({
                "identity": paper["identity"],
                "doi": paper["doi"] or "",
                "decision": decision,
            })
        report = {
            "audit_id": audit_id,
            "created_at": utc_now(),
            "status": "preview",
            "database": str(db_path.expanduser().resolve()),
            "backup": str(backup.expanduser().resolve()),
            "integrity": "ok",
            "threshold": threshold,
            "before": before,
            "planned": {
                "eligible": sum(1 for item in items if item["decision"]["eligibility_status"] == "eligible"),
                "excluded": sum(1 for item in items if item["decision"]["eligibility_status"] == "excluded"),
                "quarantine": sum(1 for item in items if item["decision"]["eligibility_status"] == "quarantine"),
                "reasons": dict(reason_counts),
            },
            "items": items,
            "restore": f"academic-radar db restore --backup {backup} --db {db_path} --replace",
        }
    finally:
        db.close()
    reports = state / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"cleanup-preview-{audit_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db = connect(db_path)
    try:
        with db:
            db.execute(
                """INSERT INTO cleanup_audits(
                audit_id,status,backup_path,report_path,before_json,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (audit_id, "preview", str(backup), str(report_path), json.dumps(before, ensure_ascii=False), report["created_at"]),
            )
    finally:
        db.close()
    return report | {"report_path": str(report_path)}


def apply_cleanup_preview(db_path: Path, state_dir: Path, report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.expanduser().read_text(encoding="utf-8"))
    if report.get("status") != "preview":
        raise ValueError("清洗报告不是可应用的预览")
    backup = Path(report["backup"])
    if not backup.exists():
        raise FileNotFoundError("清洗预览对应的备份不存在")
    check = connect(backup)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("清洗备份完整性检查失败")
    finally:
        check.close()
    db = connect(db_path)
    try:
        with db:
            for item in report["items"]:
                decision = item["decision"]
                db.execute(
                    """UPDATE papers SET publication_type=?,eligibility_status=?,exclusion_reason=?,
                    publication_type_evidence_json=? WHERE identity=?""",
                    (
                        decision["publication_type"], decision["eligibility_status"],
                        decision.get("exclusion_reason"), json.dumps(decision.get("evidence", []), ensure_ascii=False),
                        item["identity"],
                    ),
                )
            applied_at = utc_now()
            db.execute(
                "UPDATE cleanup_audits SET status='applied',applied_at=? WHERE audit_id=? AND status='preview'",
                (applied_at, report["audit_id"]),
            )
    finally:
        db.close()
    after = governance_stats(db_path, float(report["threshold"]))
    report["status"] = "applied"
    report["applied_at"] = applied_at
    report["after"] = after
    applied_path = Path(state_dir).expanduser().resolve() / "reports" / f"cleanup-applied-{report['audit_id']}.json"
    applied_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db = connect(db_path)
    try:
        with db:
            db.execute(
                "UPDATE cleanup_audits SET report_path=?,after_json=? WHERE audit_id=?",
                (str(applied_path), json.dumps(after, ensure_ascii=False), report["audit_id"]),
            )
    finally:
        db.close()
    return report | {"report_path": str(applied_path)}
