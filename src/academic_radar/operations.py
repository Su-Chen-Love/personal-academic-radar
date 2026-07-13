"""Operational verification for a daily Personal Academic Radar installation."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .storage import connect, database_status, upgrade_database


WEB_SERVICE_LABEL="com.personal-academic-radar.web"


def _web_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/healthz",timeout=2) as response:
            return response.status==200
    except Exception:
        return False


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


def _state_for_config(config_path: Path) -> Path:
    with config_path.expanduser().open("rb") as handle: config=tomllib.load(handle)
    raw=Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent/raw).resolve()


def install_web_service(
    config_path: Path,
    *,
    launch_agents_dir: Path | None=None,
    python_executable: Path | None=None,
    activate: bool=True,
) -> dict[str, Any]:
    config_path=config_path.expanduser().resolve(); state=_state_for_config(config_path)
    state.mkdir(parents=True,exist_ok=True)
    launch_agents=(launch_agents_dir or Path.home()/"Library/LaunchAgents").expanduser().resolve()
    launch_agents.mkdir(parents=True,exist_ok=True)
    plist_path=launch_agents/f"{WEB_SERVICE_LABEL}.plist"
    # Preserve a virtual-environment symlink. Resolving it would collapse to the
    # system interpreter, which cannot see packages installed in the venv.
    python_path=(python_executable or Path(sys.executable)).expanduser().absolute()
    if not python_path.exists(): raise FileNotFoundError(python_path)
    payload={
        "Label":WEB_SERVICE_LABEL,
        "ProgramArguments":[str(python_path),"-m","academic_radar.cli","web","--config",str(config_path),"--port","8765"],
        "RunAtLoad":True,
        "KeepAlive":{"SuccessfulExit":False},
        "ThrottleInterval":5,
        "ProcessType":"Background",
        "StandardOutPath":str(state/"web.stdout.log"),
        "StandardErrorPath":str(state/"web.stderr.log"),
    }
    fd,name=tempfile.mkstemp(prefix=plist_path.name+".",suffix=".tmp",dir=launch_agents)
    try:
        with os.fdopen(fd,"wb") as handle:
            plistlib.dump(payload,handle,sort_keys=False); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,plist_path)
    finally: Path(name).unlink(missing_ok=True)
    loaded=False
    if activate:
        if sys.platform!="darwin": raise RuntimeError("launchd service activation is available only on macOS")
        domain=f"gui/{os.getuid()}"
        subprocess.run(["launchctl","bootout",domain+"/"+WEB_SERVICE_LABEL],capture_output=True,text=True)
        result=subprocess.run(["launchctl","bootstrap",domain,str(plist_path)],capture_output=True,text=True)
        if result.returncode: raise RuntimeError("launchctl bootstrap failed: "+result.stderr.strip())
        loaded=True
        for _ in range(20):
            if _web_healthy(): break
            time.sleep(.25)
        if not _web_healthy(): raise RuntimeError("web service was loaded but did not become healthy; inspect web.stderr.log")
    return {"service":WEB_SERVICE_LABEL,"plist":str(plist_path),"loaded":loaded,
            "healthy":_web_healthy() if loaded else None,"config":str(config_path)}


def uninstall_web_service(*,launch_agents_dir: Path | None=None) -> dict[str, Any]:
    launch_agents=(launch_agents_dir or Path.home()/"Library/LaunchAgents").expanduser().resolve()
    plist_path=launch_agents/f"{WEB_SERVICE_LABEL}.plist"; unloaded=False
    if sys.platform=="darwin":
        result=subprocess.run(["launchctl","bootout",f"gui/{os.getuid()}/{WEB_SERVICE_LABEL}"],capture_output=True,text=True)
        unloaded=result.returncode==0
    plist_path.unlink(missing_ok=True)
    return {"service":WEB_SERVICE_LABEL,"plist":str(plist_path),"unloaded":unloaded,"removed":not plist_path.exists()}


def web_service_status() -> dict[str, Any]:
    loaded=False; process=""
    if sys.platform=="darwin":
        result=subprocess.run(["launchctl","print",f"gui/{os.getuid()}/{WEB_SERVICE_LABEL}"],capture_output=True,text=True)
        loaded=result.returncode==0; process=result.stdout[:2000] if loaded else ""
    state_match=re.search(r"\bstate = ([^\n]+)",process); pid_match=re.search(r"\bpid = (\d+)",process)
    exit_match=re.search(r"\blast exit code = ([^\n]+)",process)
    return {"service":WEB_SERVICE_LABEL,"loaded":loaded,"healthy":_web_healthy(),
            "state":state_match.group(1).strip() if state_match else None,
            "pid":int(pid_match.group(1)) if pid_match else None,
            "last_exit":exit_match.group(1).strip() if exit_match else None}
