"""Versioned SQLite storage, backup, restore, and legacy-state migration."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).with_name("migrations")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def load_migrations() -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        prefix, _, name = path.stem.partition("_")
        if not prefix.isdigit() or not name:
            raise ValueError(f"Invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(int(prefix), name, sql, hashlib.sha256(sql.encode()).hexdigest()))
    versions = [item.version for item in migrations]
    if not migrations or versions != list(range(1, len(migrations) + 1)):
        raise ValueError("Migrations must be contiguous and start at 001")
    return migrations


def _ensure_migration_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations(
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
        )"""
    )
    db.commit()


def upgrade_database(path: Path) -> dict[str, object]:
    migrations = load_migrations()
    db = connect(path)
    try:
        _ensure_migration_table(db)
        applied_rows = db.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {int(row["version"]): row for row in applied_rows}
        known = {migration.version: migration for migration in migrations}
        unknown = sorted(set(applied) - set(known))
        if unknown:
            raise RuntimeError(f"Database has unknown future migrations: {unknown}")
        for version, row in applied.items():
            migration = known[version]
            if row["checksum"] != migration.checksum:
                raise RuntimeError(f"Migration {version:03d} checksum has changed")

        newly_applied: list[int] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            values = (
                str(migration.version),
                migration.name.replace("'", "''"),
                migration.checksum.replace("'", "''"),
                utc_now().replace("'", "''"),
            )
            script = (
                "BEGIN IMMEDIATE;\n"
                + migration.sql
                + "\nINSERT INTO schema_migrations(version,name,checksum,applied_at) "
                + f"VALUES({values[0]},'{values[1]}','{values[2]}','{values[3]}');\nCOMMIT;"
            )
            try:
                db.executescript(script)
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise
            newly_applied.append(migration.version)
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(migrations[-1].version),),
        )
        db.commit()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")
        return {
            "database": str(path.expanduser().resolve()),
            "schema_version": migrations[-1].version,
            "applied": newly_applied,
            "integrity": integrity,
        }
    finally:
        db.close()


def database_status(path: Path) -> dict[str, object]:
    path = path.expanduser().resolve()
    if not path.exists():
        return {"database": str(path), "exists": False, "schema_version": 0}
    # WAL-mode backups restored without sidecars may need to create fresh
    # transient sidecars before they can be inspected.
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        version = 0
        if "schema_migrations" in tables:
            version = int(db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
        counts = {}
        for table in ("papers", "screenings", "paper_feedback", "profile_versions", "pipeline_runs"):
            if table in tables:
                counts[table] = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "database": str(path),
            "exists": True,
            "schema_version": version,
            "integrity": integrity,
            "counts": counts,
        }
    finally:
        db.close()


def backup_database(source: Path, output: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_db = sqlite3.connect(temp)
        try:
            source_db.backup(destination_db)
        finally:
            destination_db.close()
            source_db.close()
        # A copied WAL-mode database may need to create transient sidecar files
        # while it is being checked, so do not force a read-only URI here.
        check = sqlite3.connect(temp)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        os.replace(temp, output)
        return {"source": str(source), "backup": str(output), "integrity": integrity}
    finally:
        temp.unlink(missing_ok=True)


def restore_database(backup: Path, destination: Path, replace: bool = False) -> dict[str, object]:
    backup = backup.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not backup.exists():
        raise FileNotFoundError(backup)
    check = sqlite3.connect(backup)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        raise RuntimeError(f"Refusing corrupt backup: {integrity}")
    preserved = None
    if destination.exists():
        if not replace:
            raise FileExistsError("Destination exists; pass --replace to preserve and replace it")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        preserved = destination.with_name(f"{destination.stem}.pre-restore-{stamp}{destination.suffix}")
        backup_database(destination, preserved)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(backup, temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "backup": str(backup),
        "database": str(destination),
        "integrity": integrity,
        "preserved_previous": str(preserved) if preserved else None,
    }


def migrate_state(source: Path, destination: Path, merge: bool = False) -> dict[str, object]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if source == destination:
        raise ValueError("Source and destination must differ")
    if destination.exists() and any(destination.iterdir()) and not merge:
        raise FileExistsError("Destination is not empty; pass --merge to preserve existing files")
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in {"__pycache__", ".git"} for part in relative.parts):
            continue
        if path.name in {".DS_Store", "papers.sqlite3", "papers.sqlite3-shm", "papers.sqlite3-wal"}:
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif target.exists():
            skipped.append(str(relative))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(str(relative))
    source_db = source / "papers.sqlite3"
    destination_db = destination / "papers.sqlite3"
    if source_db.exists() and not destination_db.exists():
        backup_database(source_db, destination_db)
    elif source_db.exists() and destination_db.exists():
        skipped.append("papers.sqlite3")
    upgrade = upgrade_database(destination_db)
    profile_seeded = False
    profile_path = destination / "research-profile.md"
    if profile_path.exists():
        content = profile_path.read_text(encoding="utf-8")
        profile_hash = hashlib.sha256(content.encode()).hexdigest()
        db = connect(destination_db)
        try:
            if not db.execute("SELECT 1 FROM profile_versions WHERE status='active'").fetchone():
                with db:
                    db.execute(
                        """INSERT OR IGNORE INTO profile_versions(
                        profile_hash,content,status,source,change_summary,created_at,confirmed_at
                        ) VALUES(?,?,'active','legacy_import',?,?,?)""",
                        (profile_hash, content, "Imported from the existing active profile", utc_now(), utc_now()),
                    )
                profile_seeded = True
        finally:
            db.close()
    manifest = {
        "source": str(source),
        "destination": str(destination),
        "migrated_at": utc_now(),
        "copied": copied,
        "skipped": skipped,
        "database": upgrade,
        "profile_seeded": profile_seeded,
    }
    (destination / "migration-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
