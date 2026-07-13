"""Local-first web application for the Personal Academic Radar."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sqlite3
import urllib.parse
import urllib.request
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
    import tomli as tomllib  # type: ignore

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .engagement import confirm_profile, create_profile_draft, set_feedback
from .storage import connect, database_status, upgrade_database


PACKAGE_DIR = Path(__file__).resolve().parent


def load_config(path: Path) -> dict[str, Any]:
    with path.expanduser().open("rb") as handle:
        return tomllib.load(handle)


def resolve_state(config_path: Path, config: dict[str, Any]) -> Path:
    raw = Path(str(config["state_dir"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (config_path.parent / raw).resolve()


def rows(db: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, parameters)]


def row(db: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    value = db.execute(query, parameters).fetchone()
    return dict(value) if value else None


async def form_data(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        raise HTTPException(415, "Expected a form submission")
    parsed = urllib.parse.parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def safe_return(value: str | None, default: str = "/") -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else default


def normalize_source(data: dict[str, str]) -> dict[str, Any]:
    source_type=data.get("type","")
    source={"name":data.get("name","").strip(),"type":source_type,"required":True}
    if not source["name"]: raise ValueError("来源名称不能为空")
    if source_type=="crossref":
        issn=data.get("issn","").strip()
        if not issn: raise ValueError("期刊来源需要 ISSN")
        source["issn"]=issn
        if data.get("openalex_id","").strip(): source["openalex_id"]=data["openalex_id"].strip()
    elif source_type=="crossref-query":
        query=data.get("query_container","").strip()
        if not query: raise ValueError("会议来源需要出版物名称查询")
        source["query_container"]=query
        source["container_title_contains"]=data.get("container_title_contains","").strip() or query
        source["exclude_container_contains"]=["extended abstracts"]
    else: raise ValueError("不支持的来源类型")
    return source


def preview_source(source: dict[str, Any], user_agent: str, lookback_days: int) -> dict[str, Any]:
    since=(dt.date.today()-dt.timedelta(days=lookback_days)).isoformat()
    params={"rows":"5","select":"DOI,title,container-title,published-online,published-print,created,URL",
            "filter":f"from-created-date:{since}","sort":"created","order":"desc"}
    if source["type"]=="crossref":
        endpoint="https://api.crossref.org/journals/"+urllib.parse.quote(source["issn"])+"/works"
    else:
        endpoint="https://api.crossref.org/works"; params["query.container-title"]=source["query_container"]
        params["filter"] += ",prefix:10.1145,type:proceedings-article"
    request=urllib.request.Request(endpoint+"?"+urllib.parse.urlencode(params),headers={"User-Agent":user_agent})
    with urllib.request.urlopen(request,timeout=20) as response:
        message=json.loads(response.read().decode("utf-8")).get("message",{})
    samples=[]
    for item in message.get("items",[]):
        title=" ".join(item.get("title") or []).strip(); venue=" ".join(item.get("container-title") or []).strip()
        if source["type"]=="crossref-query":
            if source["container_title_contains"].lower() not in venue.lower(): continue
            if any(value.lower() in venue.lower() for value in source["exclude_container_contains"]): continue
        if title: samples.append({"title":title,"venue":venue,"doi":item.get("DOI","")})
    return {"source":source,"total_results":int(message.get("total-results",0)),"samples":samples,"since":since}


def _toml_text(value: Any) -> str:
    if isinstance(value,bool): return "true" if value else "false"
    if isinstance(value,list): return "["+", ".join(_toml_text(item) for item in value)+"]"
    return json.dumps(str(value),ensure_ascii=False)


def write_sources(config_path: Path, sources: list[dict[str, Any]]) -> Path:
    original=config_path.read_text(encoding="utf-8")
    prefix=original.split("[[sources]]",1)[0].rstrip()+"\n\n"
    blocks=[]
    order=("name","type","required","issn","openalex_id","query_container","container_title_contains","exclude_container_contains")
    for source in sources:
        lines=["[[sources]]"]+[f"{key} = {_toml_text(source[key])}" for key in order if key in source]
        blocks.append("\n".join(lines))
    content=prefix+"\n\n".join(blocks)+"\n"
    stamp=dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup=config_path.with_name(f"{config_path.name}.backup-{stamp}")
    backup.write_text(original,encoding="utf-8")
    fd,name=tempfile.mkstemp(prefix=config_path.name+".",suffix=".tmp",dir=config_path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,config_path)
    finally: Path(name).unlink(missing_ok=True)
    return backup


def create_app(config_path: Path) -> FastAPI:
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    state = resolve_state(config_path, config)
    db_path = state / "papers.sqlite3"
    upgrade_database(db_path)

    app = FastAPI(title="Personal Academic Radar", docs_url=None, redoc_url=None)
    app.state.config_path = config_path
    app.state.config = config
    app.state.state = state
    app.state.db_path = db_path
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.pending_sources = {}
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response=await call_next(request)
        response.headers["Content-Security-Policy"]="default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["X-Frame-Options"]="DENY"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> PlainTextResponse:
        return PlainTextResponse(f"无法保存：{exc}", status_code=400)

    def context(request: Request, page: str, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "page": page,
            "csrf_token": app.state.csrf_token,
            "state_path": str(state),
            **values,
        }

    def validate_csrf(data: dict[str, str]) -> None:
        if not secrets.compare_digest(data.get("csrf_token", ""), app.state.csrf_token):
            raise HTTPException(403, "Invalid form token")

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        status = database_status(db_path)
        return {"ok": status.get("integrity") == "ok", "schema_version": status.get("schema_version")}

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        db = connect(db_path)
        try:
            active = row(db, "SELECT * FROM profile_versions WHERE status='active'")
            profile_hash = active["profile_hash"] if active else ""
            papers = rows(db, """WITH latest AS (
              SELECT s.*,ROW_NUMBER() OVER(PARTITION BY s.identity ORDER BY s.screened_at DESC) AS rn
              FROM screenings s WHERE s.profile_hash=? AND s.provider='codex-agent'
            ) SELECT p.*,l.score,l.reasons,l.confidence,l.screened_at,l.themes_json,
              f.interest,f.reason AS feedback_reason,COALESCE(f.favorite,0) favorite,
              COALESCE(f.reading_status,'unread') reading_status
              FROM latest l JOIN papers p ON p.identity=l.identity
              LEFT JOIN paper_feedback f ON f.identity=p.identity
              WHERE l.rn=1 AND l.relevant=1 AND date(l.screened_at)=date('now')
              ORDER BY l.score DESC,p.published DESC""", (profile_hash,))
            latest_run = row(db, "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1")
            totals = row(db, """SELECT COUNT(*) papers,
              (SELECT COUNT(*) FROM paper_feedback WHERE favorite=1) favorites,
              (SELECT COUNT(*) FROM paper_feedback WHERE reading_status='read_later') read_later,
              (SELECT COUNT(*) FROM source_health WHERE status='healthy') healthy_sources
              FROM papers""")
            return templates.TemplateResponse(request,"today.html",context(request,"today",papers=papers,
                latest_run=latest_run,totals=totals,active_profile=active))
        finally:
            db.close()

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, q: str = "", interest: str = "", reading: str = "", page_no: int = 1) -> HTMLResponse:
        page_no=max(1,page_no); limit=24; offset=(page_no-1)*limit
        clauses=["1=1"]; parameters: list[Any]=[]
        if q:
            clauses.append("(p.title LIKE ? OR p.abstract LIKE ? OR p.venue LIKE ?)")
            value=f"%{q}%"; parameters.extend([value,value,value])
        if interest in ("interested","not_interested"):
            clauses.append("f.interest=?"); parameters.append(interest)
        if reading in ("unread","read","read_later"):
            clauses.append("COALESCE(f.reading_status,'unread')=?"); parameters.append(reading)
        where=" AND ".join(clauses)
        db=connect(db_path)
        try:
            active=row(db,"SELECT profile_hash FROM profile_versions WHERE status='active'") or {"profile_hash":""}
            latest="""SELECT * FROM (SELECT s.*,ROW_NUMBER() OVER(PARTITION BY s.identity ORDER BY s.screened_at DESC) rn
              FROM screenings s WHERE s.profile_hash=?) WHERE rn=1"""
            query=f"""SELECT p.*,s.score,s.relevant,s.reasons,f.interest,f.reason AS feedback_reason,
              COALESCE(f.favorite,0) favorite,COALESCE(f.reading_status,'unread') reading_status
              FROM papers p LEFT JOIN ({latest}) s ON s.identity=p.identity
              LEFT JOIN paper_feedback f ON f.identity=p.identity WHERE {where}
              ORDER BY p.published DESC,p.first_seen DESC LIMIT ? OFFSET ?"""
            paper_rows=rows(db,query,tuple([active["profile_hash"]]+parameters+[limit,offset]))
            count=row(db,f"""SELECT COUNT(*) count FROM papers p LEFT JOIN paper_feedback f ON f.identity=p.identity
              WHERE {where}""",tuple(parameters))["count"]
            return templates.TemplateResponse(request,"library.html",context(request,"library",papers=paper_rows,
                q=q,interest=interest,reading=reading,page_no=page_no,total=count,has_next=offset+limit<count))
        finally: db.close()

    def render_sources(request: Request, preview: dict[str,Any] | None=None) -> HTMLResponse:
        db=connect(db_path)
        try:
            health={item["source"]:item for item in rows(db,"SELECT * FROM source_health")}
            latest={item["source"]:item for item in rows(db,"""SELECT sr.* FROM source_runs sr JOIN (
              SELECT source,MAX(finished_at) finished_at FROM source_runs GROUP BY source
            ) x ON x.source=sr.source AND x.finished_at=sr.finished_at""")}
            items=[]
            for source in config.get("sources",[]):
                items.append({**source,"health":health.get(source["name"],{"status":"unknown"}),
                              "latest":latest.get(source["name"])})
            return templates.TemplateResponse(request,"sources.html",context(request,"sources",sources=items,preview=preview))
        finally: db.close()

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> HTMLResponse:
        return render_sources(request)

    @app.post("/sources/preview",response_class=HTMLResponse)
    async def source_preview(request: Request) -> HTMLResponse:
        data=await form_data(request); validate_csrf(data); source=normalize_source(data)
        if any(item["name"].lower()==source["name"].lower() for item in config.get("sources",[])):
            raise ValueError("这个来源名称已经存在")
        try:
            result=preview_source(source,config.get("user_agent","PersonalAcademicRadar/0.3"),int(config.get("lookback_days",14)))
        except Exception as exc:
            raise ValueError(f"来源预览失败：{type(exc).__name__}") from exc
        token=secrets.token_urlsafe(24); app.state.pending_sources[token]=source; result["token"]=token
        return render_sources(request,result)

    @app.post("/sources/confirm")
    async def source_confirm(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        source=app.state.pending_sources.pop(data.get("token",""),None)
        if not source: raise ValueError("预览已过期，请重新预览")
        new_sources=[*config.get("sources",[]),source]
        write_sources(config_path,new_sources); config["sources"]=new_sources
        return RedirectResponse("/sources",303)

    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request) -> HTMLResponse:
        db=connect(db_path)
        try:
            versions=rows(db,"SELECT * FROM profile_versions ORDER BY created_at DESC,id DESC")
            active=next((item for item in versions if item["status"]=="active"),None)
            return templates.TemplateResponse(request,"profile.html",context(request,"profile",versions=versions,active=active))
        finally: db.close()

    @app.post("/profile/draft")
    async def profile_draft(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        create_profile_draft(db_path,data.get("content",""),data.get("summary",""),"web")
        return RedirectResponse("/profile",303)

    @app.post("/profile/confirm")
    async def profile_confirm(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        confirm_profile(db_path,int(data["version_id"]),state/config["profile_file"])
        return RedirectResponse("/profile",303)

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback(request: Request) -> HTMLResponse:
        db=connect(db_path)
        try:
            items=rows(db,"""SELECT f.*,p.title,p.venue FROM paper_feedback f JOIN papers p ON p.identity=f.identity
              ORDER BY f.updated_at DESC""")
            events=rows(db,"""SELECT e.*,p.title FROM feedback_events e JOIN papers p ON p.identity=e.identity
              ORDER BY e.created_at DESC LIMIT 100""")
            stats=row(db,"""SELECT COUNT(*) total,SUM(interest='interested') interested,
              SUM(interest='not_interested') not_interested,SUM(favorite) favorites,
              SUM(reading_status='read') was_read FROM paper_feedback""")
            return templates.TemplateResponse(request,"feedback.html",context(request,"feedback",items=items,events=events,stats=stats))
        finally: db.close()

    @app.post("/feedback")
    async def update_feedback(request: Request) -> RedirectResponse:
        data=await form_data(request); validate_csrf(data)
        interest=data.get("interest") or None
        set_feedback(db_path,data["identity"],interest,data.get("reason",""),data.get("favorite")=="on",
                     data.get("reading_status","unread"))
        return RedirectResponse(safe_return(data.get("return_to"),"/library"),303)

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request) -> HTMLResponse:
        db=connect(db_path)
        try:
            runs=rows(db,"SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 30")
            jobs=rows(db,"SELECT * FROM agent_jobs ORDER BY created_at DESC LIMIT 30")
            source_runs=rows(db,"SELECT * FROM source_runs ORDER BY finished_at DESC LIMIT 50")
            db_state=database_status(db_path)
            return templates.TemplateResponse(request,"status.html",context(request,"status",runs=runs,jobs=jobs,
                source_runs=source_runs,db_state=db_state))
        finally: db.close()

    return app
