"""User-facing product helpers for setup, source discovery, and paper assets."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .engagement import seed_active_profile
from .storage import backup_database, connect, database_status, latest_schema_version, upgrade_database, utc_now


DEFAULT_STATE_DIR = Path("~/.local/share/personal-academic-radar")
MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_PROFILE = """# Research interest profile

## Core problem

Describe the research problem this radar should prioritize.

## High-priority themes

- Human-AI collaboration with concrete decision support value.
- Preference elicitation, mixed-initiative interaction, and interactive optimization.
- Methods or evaluations that can transfer to the user's active research.

## Methodological interests

- Empirical studies with clear tasks, measures, and operational settings.
- Systems that expose uncertainty, clarification, grounding, or calibration.

## Relevance boundaries

- Venue match alone is not enough.
- Generic editorials, announcements, board notes, and book reviews are low priority.

## Feedback history

Record false positives and false negatives here as the radar improves.
"""


LOW_PRIORITY_PATTERNS = [
    (r"\beditorial\b", "editorial item"),
    (r"\beditors?'?\s+note\b", "editor note"),
    (r"\beditorial\s+board\b", "editorial board item"),
    (r"\bcorrection\b|\berratum\b", "correction or erratum"),
    (r"\bannouncement\b", "announcement"),
    (r"\bbook\s+review\b", "book review"),
    (r"\bcall\s+for\s+papers\b", "call for papers"),
    (r"\bfront\s+matter\b|\bback\s+matter\b", "front/back matter"),
]


def asset_text(name: str, fallback: str) -> str:
    # Installed wheels carry their own copy.  The repository-level copy remains
    # useful for people reading or adapting the examples before installation.
    packaged = Path(__file__).with_name("assets") / name
    repository = Path(__file__).resolve().parents[2] / "assets" / name
    for path in (packaged, repository):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return fallback


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def load_config(path: Path) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        return tomllib.load(handle)


def migrate_legacy_model_config(path: Path) -> str | None:
    """Remove obsolete direct-model settings after preserving the full file."""

    path = path.expanduser().resolve()
    if not path.exists():
        return None
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    kept: list[str] = []
    in_llm = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[llm]":
            in_llm = True
            removed = True
            continue
        if in_llm and stripped.startswith("[") and stripped.endswith("]"):
            in_llm = False
        if not in_llm:
            kept.append(line)
    if not removed:
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.backup-pre-v0.8-{stamp}")
    shutil.copy2(path, backup)
    _atomic_write(path, "".join(kept))
    return str(backup)


def resolve_state(config_path: Path, config: dict[str, Any]) -> Path:
    raw = Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def initialize_installation(state_dir: Path = DEFAULT_STATE_DIR, config_path: Path | None = None) -> dict[str, Any]:
    state = state_dir.expanduser().resolve()
    config = (config_path.expanduser().resolve() if config_path else state / "config.toml")
    state.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    preserved: list[str] = []

    legacy_config_backup = None
    if config.exists():
        preserved.append(str(config))
        legacy_config_backup = migrate_legacy_model_config(config)
    else:
        text = asset_text("config.example.toml", 'state_dir = "."\nprofile_file = "research-profile.md"\n')
        if config.parent == state:
            text = re.sub(r'^state_dir\s*=.*$', 'state_dir = "."', text, count=1, flags=re.M)
        else:
            text = re.sub(r'^state_dir\s*=.*$', f"state_dir = {json.dumps(str(state))}", text, count=1, flags=re.M)
        _atomic_write(config, text)
        created.append(str(config))

    cfg = load_config(config)
    resolved_state = resolve_state(config, cfg)
    resolved_state.mkdir(parents=True, exist_ok=True)
    profile = resolved_state / cfg.get("profile_file", "research-profile.md")
    if profile.exists():
        preserved.append(str(profile))
    else:
        _atomic_write(profile, asset_text("research-profile.example.md", DEFAULT_PROFILE))
        created.append(str(profile))

    db_path = resolved_state / "papers.sqlite3"
    pre_upgrade_backup = None
    if db_path.exists():
        status = database_status(db_path)
        if not status.get("schema_current", False):
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = resolved_state / "backups" / f"pre-upgrade-v{status.get('schema_version', 0)}-{stamp}.sqlite3"
            backup_database(db_path, backup_path)
            pre_upgrade_backup = str(backup_path)
    upgrade = upgrade_database(db_path)
    metadata_repaired = repair_product_metadata(db_path)
    active = seed_active_profile(db_path, profile.read_text(encoding="utf-8"), "init")
    return {
        "ok": True,
        "state_dir": str(resolved_state),
        "config": str(config),
        "database": str(db_path),
        "profile": str(profile),
        "created": created,
        "preserved": preserved,
        "schema_version": upgrade["schema_version"],
        "active_profile_id": active["id"],
        "pre_upgrade_backup": pre_upgrade_backup,
        "legacy_config_backup": legacy_config_backup,
        "metadata_repaired": metadata_repaired,
    }


def classify_low_priority(title: str, venue: str = "") -> tuple[bool, str]:
    text = f"{title} {venue}".lower()
    for pattern, reason in LOW_PRIORITY_PATTERNS:
        if re.search(pattern, text):
            return True, reason
    return False, ""


def repair_product_metadata(db_path: Path) -> dict[str, int]:
    """Backfill user-facing metadata introduced after early local releases."""

    database = connect(db_path)
    abstract_sources = 0
    low_priority = 0
    confidence_capped = 0
    try:
        paper_rows = database.execute(
            "SELECT identity,title,venue,abstract,abstract_source,low_priority FROM papers"
        ).fetchall()
        with database:
            for paper in paper_rows:
                updates: dict[str, Any] = {}
                if paper["abstract_source"] in {None, "", "unknown"}:
                    updates["abstract_source"] = "existing" if (paper["abstract"] or "").strip() else "missing"
                    abstract_sources += 1
                if not paper["low_priority"]:
                    is_low, reason = classify_low_priority(paper["title"], paper["venue"] or "")
                    if is_low:
                        updates["low_priority"] = 1
                        updates["low_priority_reason"] = reason
                        low_priority += 1
                if updates:
                    assignments = ",".join(f"{column}=?" for column in updates)
                    database.execute(
                        f"UPDATE papers SET {assignments} WHERE identity=?",
                        (*updates.values(), paper["identity"]),
                    )
            cursor = database.execute(
                """UPDATE screenings SET confidence=0.5
                WHERE identity IN (SELECT identity FROM papers WHERE COALESCE(abstract,'')='')
                  AND (confidence IS NULL OR confidence>0.5)"""
            )
            confidence_capped = max(0, cursor.rowcount)
        return {
            "abstract_sources": abstract_sources,
            "low_priority": low_priority,
            "confidence_capped": confidence_capped,
        }
    finally:
        database.close()


def abstract_source_for(abstract: str, current: str = "") -> str:
    if abstract:
        return current if current and current not in {"missing", "unknown"} else "metadata"
    return "missing"


def source_coverage(db_path: Path, configured_sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    db = connect(db_path)
    try:
        output: dict[str, dict[str, Any]] = {}
        for source in configured_sources:
            name = source["name"]
            rows = db.execute(
                """SELECT DISTINCT p.identity,p.published,p.abstract,p.first_seen FROM observations o
                JOIN papers p ON p.identity=o.identity
                WHERE o.source=? OR o.source=?""",
                (name, name + " / OpenAlex"),
            ).fetchall()
            count = len(rows)
            abstracts = sum(1 for item in rows if item["abstract"])
            dates = [item["published"] for item in rows if item["published"]]
            first_seen = [item["first_seen"] for item in rows if item["first_seen"]]
            output[name] = {
                "paper_count": count,
                "abstract_count": abstracts,
                "abstract_percent": round((abstracts / count * 100), 1) if count else 0,
                "oldest_published": min(dates) if dates else "",
                "newest_published": max(dates) if dates else "",
                "first_seen": min(first_seen) if first_seen else "",
                "last_seen": max(first_seen) if first_seen else "",
            }
        return output
    finally:
        db.close()


def overall_quality(db_path: Path) -> dict[str, Any]:
    db = connect(db_path)
    try:
        row = db.execute(
            """SELECT COUNT(*) papers,
            SUM(CASE WHEN COALESCE(abstract,'')<>'' THEN 1 ELSE 0 END) abstracts,
            SUM(COALESCE(low_priority,0)) low_priority
            FROM papers"""
        ).fetchone()
        papers = int(row["papers"] or 0)
        abstracts = int(row["abstracts"] or 0)
        return {
            "papers": papers,
            "abstracts": abstracts,
            "abstract_percent": round(abstracts / papers * 100, 1) if papers else 0,
            "low_priority": int(row["low_priority"] or 0),
            "missing_abstracts": papers - abstracts,
        }
    finally:
        db.close()


def safe_slug(value: str, fallback: str = "paper") -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (slug[:80] or fallback).strip("-")


def import_fulltext(db_path: Path, state_dir: Path, identity: str, original_name: str, content: bytes) -> dict[str, Any]:
    if not content.startswith(b"%PDF"):
        raise ValueError("文件不是有效的 PDF，请重新选择")
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("PDF 文件超过 50 MB，请先压缩后再导入")
    db = connect(db_path)
    try:
        paper = db.execute("SELECT * FROM papers WHERE identity=?", (identity,)).fetchone()
        if not paper:
            raise ValueError(f"Unknown paper identity: {identity}")
        digest = hashlib.sha256(content).hexdigest()
        existing = db.execute(
            "SELECT * FROM fulltext_files WHERE identity=? AND sha256=?", (identity, digest)
        ).fetchone()
        if existing:
            return dict(existing) | {"deduplicated": True}
        shared = db.execute(
            "SELECT * FROM fulltext_files WHERE sha256=? ORDER BY imported_at LIMIT 1", (digest,)
        ).fetchone()
        if shared:
            now = utc_now()
            with db:
                db.execute(
                    """INSERT INTO fulltext_files(
                    identity,stored_path,original_name,sha256,size_bytes,imported_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (identity, shared["stored_path"], original_name or "paper.pdf", digest, len(content), now),
                )
            linked = db.execute(
                "SELECT * FROM fulltext_files WHERE identity=? AND sha256=?", (identity, digest)
            ).fetchone()
            return dict(linked) | {"deduplicated": True, "reused_file": True}
        year = (paper["published"] or "unknown")[:4] if paper["published"] else "unknown"
        title = safe_slug(paper["title"])
        short_hash = digest[:12]
        filename = f"{year}-{title}-{short_hash}.pdf"
        directory = state_dir.expanduser().resolve() / "fulltexts"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        fd, temp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        now = utc_now()
        with db:
            db.execute(
                """INSERT INTO fulltext_files(identity,stored_path,original_name,sha256,size_bytes,imported_at)
                VALUES(?,?,?,?,?,?)""",
                (identity, str(target), original_name or "paper.pdf", digest, len(content), now),
            )
        row = db.execute("SELECT * FROM fulltext_files WHERE sha256=?", (digest,)).fetchone()
        return dict(row) | {"deduplicated": False}
    finally:
        db.close()


def profile_assistant_prompt() -> str:
    return """请根据我提供的论文全文、摘要、关键词、研究问题、贡献、方法、实验设置、结果、局限和未来工作，生成 Personal Academic Radar 可用的研究画像草稿。

输出必须包含以下 Markdown 小节：

1. Core problem：用一段话描述我真正关心的研究问题。
2. High-priority themes：列出 4-8 个高优先级主题，主题要包含对象、方法和应用语境。
3. Methodological interests：总结可迁移的方法、实验范式、测量指标或系统设计模式。
4. Relevance boundaries：列出应排除或降低优先级的近邻主题。
5. Feedback history：如果我给了正负例，请总结 false positive / false negative 边界。

要求：
- 不要堆关键词。
- 区分“我自己的研究兴趣”和论文 related work 中只是被讨论的方向。
- 保留负向边界和不感兴趣的内容。
- 不要把 venue 或作者声望当作相关性证据。
- 输出是一份可直接粘贴到系统研究画像页面的草稿。"""


def source_candidates(query: str, user_agent: str = "PersonalAcademicRadar/0.8") -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    candidates: dict[str, dict[str, Any]] = {}
    providers_ok = 0

    def add(item: dict[str, Any]) -> None:
        name = item.get("name", "").strip()
        issn = item.get("issn", "").strip()
        openalex_id = item.get("openalex_id", "").strip()
        if not name or not (issn or openalex_id):
            return
        key = issn or openalex_id or name.lower()
        merged = {**candidates.get(key, {}), **item}
        merged["candidate_id"] = hashlib.sha256(
            f"{merged.get('name','')}|{merged.get('issn','')}|{merged.get('openalex_id','')}".encode()
        ).hexdigest()[:16]
        candidates[key] = merged

    crossref_url = "https://api.crossref.org/journals?" + urllib.parse.urlencode({"query": query, "rows": 5})
    try:
        request = urllib.request.Request(crossref_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        providers_ok += 1
        for item in data.get("message", {}).get("items", []):
            issns = item.get("ISSN") or []
            add({
                "name": item.get("title", ""),
                "issn": issns[0] if issns else "",
                "issns": issns,
                "publisher": item.get("publisher", ""),
                "source_type": "期刊",
                "config_type": "crossref",
                "match_basis": "Crossref 期刊名称与 ISSN",
            })
    except Exception:
        pass

    openalex_url = "https://api.openalex.org/sources?" + urllib.parse.urlencode({"search": query, "per_page": 8})
    try:
        request = urllib.request.Request(openalex_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        providers_ok += 1
        for item in data.get("results", []):
            issn = (item.get("issn") or [item.get("issn_l") or ""])[0]
            raw_type = str(item.get("type") or "").lower()
            if raw_type not in {"journal", "conference"}:
                continue
            add({
                "name": item.get("display_name", ""),
                "issn": issn or item.get("issn_l", ""),
                "issns": item.get("issn") or [],
                "publisher": item.get("host_organization_name", ""),
                "openalex_id": str(item.get("id", "")).rsplit("/", 1)[-1],
                "source_type": "会议" if raw_type == "conference" else "期刊",
                "config_type": "crossref" if issn and raw_type == "journal" else "openalex",
                "match_basis": "OpenAlex 已验证来源类型与名称匹配",
            })
    except Exception:
        pass

    if not providers_ok:
        raise RuntimeError("Crossref 和 OpenAlex 当前都无法响应，请检查网络后重试")
    return list(candidates.values())[:8]


def human_time(value: str | None) -> str:
    if not value:
        return "尚无记录"
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")
