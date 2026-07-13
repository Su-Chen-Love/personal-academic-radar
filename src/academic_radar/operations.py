"""Operational verification for a daily Personal Academic Radar installation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .storage import connect, database_status, upgrade_database


def verify_installation(config_path: Path) -> dict[str, Any]:
    config_path=config_path.expanduser().resolve()
    with config_path.open("rb") as handle: config=tomllib.load(handle)
    state_raw=Path(str(config["state_dir"])).expanduser()
    state=state_raw.resolve() if state_raw.is_absolute() else (config_path.parent/state_raw).resolve()
    db_path=state/"papers.sqlite3"; upgrade_database(db_path)
    checks=[]

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name":name,"ok":bool(ok),"detail":detail})

    status=database_status(db_path)
    check("database_integrity",status.get("integrity")=="ok",str(status.get("integrity")))
    profile_file=state/config["profile_file"]
    db=connect(db_path)
    try:
        active=db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
        digest=hashlib.sha256(profile_file.read_bytes()).hexdigest() if profile_file.exists() else ""
        check("confirmed_profile",bool(active) and active["profile_hash"]==digest,
              f"active={active['id'] if active else None}, file={profile_file}")
        source_rows=db.execute("SELECT source,status,last_success_at FROM source_health").fetchall()
        configured={source["name"] for source in config.get("sources",[])}
        observed={item["source"] for item in source_rows}
        unhealthy=[item["source"] for item in source_rows if item["status"] not in ("healthy","degraded")]
        check("source_coverage",configured==observed,f"configured={len(configured)}, observed={len(observed)}")
        check("source_health",not unhealthy,"unhealthy="+(", ".join(unhealthy) if unhealthy else "none"))
        latest_job=db.execute("SELECT * FROM agent_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        check("latest_semantic_job",bool(latest_job) and latest_job["status"]=="imported",
              f"run={latest_job['run_id'] if latest_job else None}, status={latest_job['status'] if latest_job else None}")
        paper_count=db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        screened_count=db.execute("""SELECT COUNT(DISTINCT identity) FROM screenings
          WHERE provider='codex-agent' AND profile_hash=(SELECT profile_hash FROM profile_versions WHERE status='active')""").fetchone()[0]
        check("semantic_coverage",paper_count==screened_count,f"papers={paper_count}, screened={screened_count}")
    finally: db.close()
    return {"ok":all(item["ok"] for item in checks),"state_dir":str(state),"checks":checks,"database":status}

