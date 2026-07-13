"""Feedback and explicitly confirmed research-profile version services."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .storage import connect, utc_now, upgrade_database


INTEREST_VALUES = {None, "interested", "not_interested"}
READING_VALUES = {"unread", "read", "read_later"}


def profile_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def seed_active_profile(db_path: Path, content: str, source: str = "initial") -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        active = db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        if active:
            return dict(active)
        now = utc_now()
        with db:
            db.execute(
                """INSERT INTO profile_versions(
                profile_hash,content,status,source,change_summary,created_at,confirmed_at
                ) VALUES(?,?,'active',?,'Initial confirmed profile',?,?)""",
                (profile_hash(content), content, source, now, now),
            )
        return dict(db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone())
    finally:
        db.close()


def active_profile(db_path: Path) -> dict[str, Any] | None:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        row = db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def create_profile_draft(
    db_path: Path, content: str, change_summary: str, source: str = "manual"
) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("Profile content must not be empty")
    if not change_summary.strip():
        raise ValueError("A change summary is required")
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        digest = profile_hash(content)
        existing = db.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (digest,)).fetchone()
        if existing:
            return dict(existing)
        with db:
            db.execute(
                """INSERT INTO profile_versions(
                profile_hash,content,status,source,change_summary,created_at,confirmed_at
                ) VALUES(?,?,'draft',?,?,?,NULL)""",
                (digest, content, source, change_summary.strip(), utc_now()),
            )
        return dict(db.execute("SELECT * FROM profile_versions WHERE profile_hash=?", (digest,)).fetchone())
    finally:
        db.close()


def _atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def confirm_profile(db_path: Path, version_id: int, profile_file: Path) -> dict[str, Any]:
    upgrade_database(db_path)
    db = connect(db_path)
    previous_content = profile_file.read_text(encoding="utf-8") if profile_file.exists() else None
    try:
        candidate = db.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone()
        if not candidate:
            raise ValueError(f"Unknown profile version: {version_id}")
        if candidate["status"] != "draft":
            raise ValueError("Only a draft profile can be confirmed")
        _atomic_write(profile_file, candidate["content"])
        try:
            with db:
                db.execute("UPDATE profile_versions SET status='superseded' WHERE status='active'")
                db.execute(
                    "UPDATE profile_versions SET status='active',confirmed_at=? WHERE id=? AND status='draft'",
                    (utc_now(), version_id),
                )
        except Exception:
            if previous_content is None:
                profile_file.unlink(missing_ok=True)
            else:
                _atomic_write(profile_file, previous_content)
            raise
        return dict(db.execute("SELECT * FROM profile_versions WHERE id=?", (version_id,)).fetchone())
    finally:
        db.close()


def list_profiles(db_path: Path) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        return [dict(row) for row in db.execute(
            "SELECT * FROM profile_versions ORDER BY created_at DESC,id DESC"
        )]
    finally:
        db.close()


def set_feedback(
    db_path: Path,
    identity: str,
    interest: str | None,
    reason: str,
    favorite: bool,
    reading_status: str,
) -> dict[str, Any]:
    if interest not in INTEREST_VALUES:
        raise ValueError(f"Invalid interest value: {interest}")
    if reading_status not in READING_VALUES:
        raise ValueError(f"Invalid reading status: {reading_status}")
    if interest is not None and not reason.strip():
        raise ValueError("A reason is required for interested/not interested feedback")
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM papers WHERE identity=?", (identity,)).fetchone():
            raise ValueError(f"Unknown paper identity: {identity}")
        now = utc_now()
        with db:
            db.execute(
                """INSERT INTO paper_feedback(
                identity,interest,reason,favorite,reading_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                interest=excluded.interest,reason=excluded.reason,favorite=excluded.favorite,
                reading_status=excluded.reading_status,updated_at=excluded.updated_at""",
                (identity, interest, reason.strip() or None, int(favorite), reading_status, now, now),
            )
            db.execute(
                "INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES(?,?,?,?,?,?)",
                (identity, interest, reason.strip() or None, int(favorite), reading_status, now),
            )
        return dict(db.execute("SELECT * FROM paper_feedback WHERE identity=?", (identity,)).fetchone())
    finally:
        db.close()


def feedback_examples(db_path: Path, per_class: int = 20) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        examples: list[dict[str, Any]] = []
        for interest in ("interested", "not_interested"):
            rows = db.execute(
                """SELECT f.interest,f.reason,f.updated_at,p.identity,p.title,p.abstract,p.venue
                FROM paper_feedback f JOIN papers p ON p.identity=f.identity
                WHERE f.interest=? ORDER BY f.updated_at DESC LIMIT ?""",
                (interest, max(1, per_class)),
            ).fetchall()
            examples.extend(dict(row) for row in rows)
        examples.sort(key=lambda item: item["updated_at"], reverse=True)
        return examples
    finally:
        db.close()


def list_feedback(db_path: Path) -> list[dict[str, Any]]:
    upgrade_database(db_path)
    db = connect(db_path)
    try:
        return [dict(row) for row in db.execute(
            """SELECT f.*,p.title,p.venue FROM paper_feedback f JOIN papers p ON p.identity=f.identity
            ORDER BY f.updated_at DESC"""
        )]
    finally:
        db.close()

