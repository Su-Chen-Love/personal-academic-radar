#!/usr/bin/env python3
"""Idempotent academic paper monitor. Python 3.9+, standard library only."""
from __future__ import annotations

import argparse, datetime as dt, email.message, email.utils, hashlib, html, json, os, random, re
import smtplib, sqlite3, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

PROJECT_SRC=Path(__file__).resolve().parents[1]/"src"
if PROJECT_SRC.exists() and str(PROJECT_SRC) not in sys.path: sys.path.insert(0,str(PROJECT_SRC))
from academic_radar.enrichment import enrich_abstracts as run_enrichment
from academic_radar.governance import publication_decision
from academic_radar.product import abstract_source_for, classify_low_priority, manual_identity_for_title
from academic_radar.storage import latest_schema_version, upgrade_database

VERSION = "0.9.0"

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 compatibility without dependencies
    tomllib = None

def _toml_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return json.loads(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner: return []
        parts = re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', inner)
        return [_toml_value(x) for x in parts]
    if raw in ("true", "false"): return raw == "true"
    try: return float(raw) if "." in raw else int(raw)
    except ValueError: raise ValueError(f"Unsupported TOML value: {raw}")

def _toml_load_fallback(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}; current = root
    for line_no, line in enumerate(text.splitlines(), 1):
        line = re.sub(r'\s+#.*$', '', line).strip()
        if not line: continue
        if line.startswith("[[") and line.endswith("]] ".strip()):
            key=line[2:-2].strip(); root.setdefault(key,[]).append({}); current=root[key][-1]; continue
        if line.startswith("[") and line.endswith("]"):
            key=line[1:-1].strip(); current=root.setdefault(key,{}); continue
        if "=" not in line: raise ValueError(f"Invalid TOML at line {line_no}")
        key,raw=line.split("=",1); current[key.strip()]=_toml_value(raw)
    return root

@dataclass
class Paper:
    identity: str; doi: str; title: str; abstract: str; venue: str
    published: str; url: str; authors: list[str]; source: str
    abstract_source: str = ""; low_priority: bool = False; low_priority_reason: str = ""
    publication_type_raw: str = ""; publication_type_source: str = ""; source_kind: str = ""
    publication_type: str = ""

class AutoClosingConnection(sqlite3.Connection):
    """Close runner connections during exception unwinding as a final safeguard."""
    def __del__(self) -> None:
        try: self.close()
        except Exception: pass

def clean_text(value: Any) -> str:
    if not value: return ""
    if isinstance(value, list): value = " ".join(str(x) for x in value)
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()

def normalize_doi(value: str) -> str:
    value = urllib.parse.unquote((value or "").strip().lower())
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(".,; ")

def identity(doi: str, title: str) -> str:
    doi = normalize_doi(doi)
    if doi: return "doi:" + doi
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())
    return "title:" + hashlib.sha256(norm.encode()).hexdigest()

def date_parts(item: dict[str, Any]) -> str:
    for key in ("published-online", "published-print", "published", "created"):
        parts = item.get(key, {}).get("date-parts", [[]])[0]
        if parts:
            vals = list(parts) + [1, 1]
            try: return dt.date(int(vals[0]), int(vals[1]), int(vals[2])).isoformat()
            except ValueError: pass
    return ""

def request_json(url: str, headers: dict[str,str], timeout: int, retries: int, backoff: float,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    hdr = {**headers, **({"Content-Type":"application/json"} if data else {})}
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            status = getattr(exc, "code", None)
            transient = status in (408, 429, 500, 502, 503, 504) or status is None
            if attempt >= retries or not transient: raise
            retry_after = getattr(exc, "headers", {}).get("Retry-After") if getattr(exc, "headers", None) else None
            delay = None
            if retry_after:
                try: delay = max(0.0,float(retry_after))
                except ValueError:
                    try:
                        retry_at=email.utils.parsedate_to_datetime(retry_after)
                        delay=max(0.0,(retry_at-dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (TypeError,ValueError): pass
            if delay is None: delay = backoff * 2**attempt + random.uniform(0, max(0.1,backoff))
            time.sleep(min(delay, 60))
    raise RuntimeError("unreachable")

def config_load(path: Path) -> dict[str, Any]:
    if tomllib:
        with path.open("rb") as f: cfg = tomllib.load(f)
    else:
        cfg = _toml_load_fallback(path.read_text(encoding="utf-8"))
    required = ("state_dir", "profile_file", "sources")
    missing = [k for k in required if k not in cfg]
    if missing: raise ValueError("Missing config fields: " + ", ".join(missing))
    if not isinstance(cfg["sources"], list) or not cfg["sources"]: raise ValueError("sources must be a non-empty list")
    return cfg

def resolve_state(cfg: dict[str,Any], config_path: Path) -> Path:
    raw = Path(os.path.expandvars(os.path.expanduser(cfg["state_dir"])))
    return raw if raw.is_absolute() else (config_path.parent / raw).resolve()

def db_open(path: Path) -> sqlite3.Connection:
    upgrade_database(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, factory=AutoClosingConnection)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS papers(
      identity TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL, abstract TEXT, venue TEXT,
      published TEXT, url TEXT, authors_json TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
      abstract_source TEXT NOT NULL DEFAULT 'unknown',
      low_priority INTEGER NOT NULL DEFAULT 0 CHECK(low_priority IN (0,1)),
      low_priority_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS observations(
      identity TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(identity, source), FOREIGN KEY(identity) REFERENCES papers(identity)
    );
    CREATE TABLE IF NOT EXISTS screenings(
      identity TEXT NOT NULL, profile_hash TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
      relevant INTEGER NOT NULL, score REAL NOT NULL, reasons TEXT, themes_json TEXT,
      confidence REAL, screened_at TEXT NOT NULL, PRIMARY KEY(identity, profile_hash, provider, model)
    );
    CREATE TABLE IF NOT EXISTS notifications(
      identity TEXT PRIMARY KEY, sent_at TEXT NOT NULL, digest_path TEXT
    );
    CREATE TABLE IF NOT EXISTS source_runs(
      run_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL,
      error TEXT, finished_at TEXT NOT NULL, since TEXT, PRIMARY KEY(run_id, source)
    );
    CREATE TABLE IF NOT EXISTS source_health(
      source TEXT PRIMARY KEY, status TEXT NOT NULL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
      last_success_at TEXT, last_failure_at TEXT, last_error TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pipeline_runs(
      run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, profile_version_id INTEGER,
      started_at TEXT NOT NULL, finished_at TEXT, collected_count INTEGER NOT NULL DEFAULT 0,
      candidate_count INTEGER NOT NULL DEFAULT 0, relevant_count INTEGER NOT NULL DEFAULT 0,
      error_summary TEXT, details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS profile_versions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, profile_hash TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
      status TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', change_summary TEXT,
      created_at TEXT NOT NULL, confirmed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS agent_jobs(
      run_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL, status TEXT NOT NULL, queue_path TEXT,
      results_path TEXT, exported_count INTEGER NOT NULL DEFAULT 0, imported_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, imported_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fulltext_files(
      id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL, stored_path TEXT NOT NULL UNIQUE,
      original_name TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
      imported_at TEXT NOT NULL, FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
    CREATE INDEX IF NOT EXISTS idx_papers_seen ON papers(first_seen);
    CREATE INDEX IF NOT EXISTS idx_fulltext_identity ON fulltext_files(identity, imported_at DESC);
    """)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (str(latest_schema_version()),))
    db.commit(); return db

def crossref_collect(source: dict[str,Any], cfg: dict[str,Any], since: str) -> list[Paper]:
    c = cfg.get("collection", {}); rows = min(1000,int(c.get("rows_per_page",c.get("rows_per_source",80))))
    max_pages=max(1,int(c.get("max_pages_per_source",3)))
    base = "https://api.crossref.org"
    params: dict[str, str] = {"rows":str(rows), "select":"DOI,title,abstract,container-title,published-online,published-print,published,created,URL,author,type,ISSN"}
    filters = [f"from-created-date:{since}"]
    if source["type"] == "crossref":
        url = f"{base}/journals/{urllib.parse.quote(source['issn'])}/works"
    else:
        url = f"{base}/works"; filters.extend(["prefix:10.1145", "type:proceedings-article"])
        params["query.container-title"] = source["query_container"]
    params["filter"] = ",".join(filters); params["sort"]="created"; params["order"]="desc"; params["cursor"]="*"
    result=[]; seen=set()
    for _ in range(max_pages):
        data = request_json(url+"?"+urllib.parse.urlencode(params), {"User-Agent":cfg.get("user_agent","ResearchPaperMonitor/1.0")},
                            int(c.get("timeout_seconds",30)), int(c.get("max_retries",3)), float(c.get("backoff_seconds",2)))
        message=data.get("message",{}); items=message.get("items",[])
        for item in items:
            venue = clean_text(item.get("container-title"))
            if source["type"] == "crossref-query":
                low = venue.lower()
                expected=source.get("container_title_contains","chi conference on human factors in computing systems").lower()
                excluded=[x.lower() for x in source.get("exclude_container_contains",["extended abstracts"])]
                if expected not in low or any(x in low for x in excluded): continue
            title=clean_text(item.get("title")); doi=normalize_doi(item.get("DOI",""))
            if not title: continue
            ident=identity(doi,title)
            if ident in seen: continue
            seen.add(ident)
            authors=[clean_text(" ".join(filter(None,(a.get("given"),a.get("family"))))) for a in item.get("author",[])]
            abstract=clean_text(item.get("abstract"))
            raw_type=str(item.get("type") or "")
            decision=publication_decision(title,venue or source["name"],raw_type,"crossref")
            low=decision["eligibility_status"]!="eligible"; low_reason=decision.get("exclusion_reason") or ""
            result.append(Paper(ident,doi,title,abstract,venue or source["name"],
                                date_parts(item),item.get("URL","") or ("https://doi.org/"+doi if doi else ""),authors,source["name"],
                                abstract_source_for(abstract,"crossref"),low,low_reason,raw_type,"crossref","",
                                decision.get("publication_type") or ""))
        next_cursor=message.get("next-cursor")
        if not next_cursor or len(items)<rows: break
        params["cursor"]=next_cursor
    return result

def openalex_abstract(doi: str, cfg: dict[str,Any]) -> str:
    if not doi or not cfg.get("collection",{}).get("openalex_fallback",True): return ""
    c=cfg.get("collection",{}); url="https://api.openalex.org/works/https://doi.org/"+urllib.parse.quote(doi,safe="")
    mail = re.search(r"mailto:([^\s;)]+)", cfg.get("user_agent",""))
    if mail: url += "?mailto=" + urllib.parse.quote(mail.group(1))
    try: data=request_json(url,{"User-Agent":cfg.get("user_agent","")},int(c.get("timeout_seconds",30)),1,1)
    except Exception: return ""
    inv=data.get("abstract_inverted_index") or {}; words=[]
    for word, positions in inv.items():
        for pos in positions: words.append((pos,word))
    return " ".join(word for _,word in sorted(words))

def inverted_abstract(data: dict[str,Any]) -> str:
    words=[]
    for word,positions in (data.get("abstract_inverted_index") or {}).items():
        words.extend((pos,word) for pos in positions)
    return " ".join(word for _,word in sorted(words))

def openalex_collect(source: dict[str,Any], cfg: dict[str,Any], since: str) -> list[Paper]:
    source_id=source.get("openalex_id")
    if not source_id or not cfg.get("collection",{}).get("openalex_fallback",True): return []
    c=cfg.get("collection",{}); rows=min(100,int(c.get("rows_per_page",c.get("rows_per_source",80))))
    max_pages=max(1,int(c.get("max_pages_per_source",3)))
    params={"filter":f"primary_location.source.id:{source_id},from_publication_date:{since}",
            "sort":"publication_date:desc","per-page":str(rows),"cursor":"*"}
    mail=re.search(r"mailto:([^\s;)]+)",cfg.get("user_agent",""))
    if mail: params["mailto"]=mail.group(1)
    result=[]; seen=set()
    for _ in range(max_pages):
        data=request_json("https://api.openalex.org/works?"+urllib.parse.urlencode(params),
          {"User-Agent":cfg.get("user_agent","")},int(c.get("timeout_seconds",30)),int(c.get("max_retries",3)),float(c.get("backoff_seconds",2)))
        items=data.get("results",[])
        for item in items:
            title=clean_text(item.get("title")); doi=normalize_doi(item.get("doi",""))
            if not title: continue
            ident=identity(doi,title)
            if ident in seen: continue
            seen.add(ident)
            loc=item.get("primary_location") or {}; src=loc.get("source") or {}
            authors=[clean_text(x.get("author",{}).get("display_name")) for x in item.get("authorships",[])]
            abstract=inverted_abstract(item)
            venue=clean_text(src.get("display_name")) or source["name"]
            raw_type=str(item.get("type_crossref") or item.get("type") or "")
            source_kind=str(src.get("type") or "")
            decision=publication_decision(title,venue,raw_type,"openalex",source_kind)
            low=decision["eligibility_status"]!="eligible"; low_reason=decision.get("exclusion_reason") or ""
            result.append(Paper(ident,doi,title,abstract,venue,
              item.get("publication_date","") or "",loc.get("landing_page_url","") or ("https://doi.org/"+doi if doi else ""),authors,source["name"]+" / OpenAlex",
              abstract_source_for(abstract,"openalex"),low,low_reason,raw_type,"openalex",source_kind,
              decision.get("publication_type") or ""))
        next_cursor=(data.get("meta") or {}).get("next_cursor")
        if not next_cursor or len(items)<rows: break
        params["cursor"]=next_cursor
    return result

def extract_json(text: str) -> dict[str,Any]:
    match=re.search(r"\{.*\}",text,re.S)
    if not match: raise ValueError("model returned no JSON object")
    value=json.loads(match.group(0)); required={"relevant","score","reasons","matched_themes","confidence"}
    if not required.issubset(value): raise ValueError("model JSON missing fields")
    value["score"]=max(0,min(1,float(value["score"]))); value["confidence"]=max(0,min(1,float(value["confidence"])))
    value["relevant"]=bool(value["relevant"]); value["reasons"]=clean_text(value["reasons"])
    value["matched_themes"]=[clean_text(x) for x in value["matched_themes"]][:8]
    return value

def upsert(db: sqlite3.Connection, p: Paper, now: str) -> bool:
    exists=db.execute("SELECT 1 FROM papers WHERE identity=?",(p.identity,)).fetchone() is not None
    if not exists and p.doi:
        # A Google Scholar APA citation does not always include a DOI.  Keep
        # the title-hash identity stable when a later provider resolves that
        # same manually added paper, instead of creating a second row.
        manual_identity=manual_identity_for_title(db,p.title)
        if manual_identity:
            p.identity=manual_identity; exists=True
    if not p.low_priority:
        p.low_priority,p.low_priority_reason=classify_low_priority(p.title,p.venue)
    p.abstract_source=abstract_source_for(p.abstract,p.abstract_source)
    decision=publication_decision(p.title,p.venue,p.publication_type_raw,p.publication_type_source,p.source_kind)
    db.execute("""INSERT INTO papers(
      identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
      abstract_source,low_priority,low_priority_reason,publication_type,publication_type_raw,
      publication_type_source,publication_type_evidence_json,eligibility_status,exclusion_reason,needs_rescreen
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
      doi=CASE WHEN excluded.doi<>'' THEN excluded.doi ELSE papers.doi END,
      title=excluded.title, abstract=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract ELSE papers.abstract END,
      abstract_source=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN excluded.abstract_source ELSE papers.abstract_source END,
      venue=excluded.venue, published=excluded.published, url=excluded.url, authors_json=excluded.authors_json,
      low_priority=excluded.low_priority, low_priority_reason=excluded.low_priority_reason,
      publication_type=excluded.publication_type,publication_type_raw=excluded.publication_type_raw,
      publication_type_source=excluded.publication_type_source,
      publication_type_evidence_json=excluded.publication_type_evidence_json,
      eligibility_status=excluded.eligibility_status,exclusion_reason=excluded.exclusion_reason,
      needs_rescreen=CASE WHEN length(excluded.abstract)>length(papers.abstract) THEN 1 ELSE papers.needs_rescreen END,
      updated_at=excluded.updated_at""",
      (p.identity,p.doi,p.title,p.abstract,p.venue,p.published,p.url,json.dumps(p.authors,ensure_ascii=False),now,now,
       p.abstract_source,int(p.low_priority),p.low_priority_reason or None,decision["publication_type"],
       p.publication_type_raw or None,p.publication_type_source or None,json.dumps(decision["evidence"],ensure_ascii=False),
       decision["eligibility_status"],decision.get("exclusion_reason"),0))
    db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)",(p.identity,p.source,now)); return not exists

def render_digest(papers: list[tuple[Paper,dict[str,Any]]], failures: list[dict[str,str]], run_id: str) -> tuple[str,str]:
    md=[f"# Research paper digest — {run_id[:10]}","",f"Relevant new papers: **{len(papers)}**",""]
    for p,s in papers:
        md += [f"## [{p.title}]({p.url})",f"- Venue: {p.venue}",f"- Published: {p.published or 'unknown'}",
               f"- Relevance: {s['score']:.2f} ({s['reasons']})","",p.abstract or "*Abstract unavailable.*",""]
    if not papers: md += ["No new papers met the relevance threshold.",""]
    if failures:
        md += ["## Source warnings",""]+[f"- {x['source']}: {x['error']}" for x in failures]+[""]
    markdown="\n".join(md)
    body=[f"<h1>Research paper digest — {html.escape(run_id[:10])}</h1><p>Relevant new papers: <b>{len(papers)}</b></p>"]
    for p,s in papers:
        body += [f'<h2><a href="{html.escape(p.url)}">{html.escape(p.title)}</a></h2>',
                 f"<p><b>Venue:</b> {html.escape(p.venue)}<br><b>Published:</b> {html.escape(p.published or 'unknown')}<br><b>Relevance:</b> {s['score']:.2f} — {html.escape(s['reasons'])}</p>",
                 f"<p>{html.escape(p.abstract or 'Abstract unavailable.')}</p>"]
    if not papers: body.append("<p>No new papers met the relevance threshold.</p>")
    if failures: body.append("<h2>Source warnings</h2><ul>"+"".join(f"<li>{html.escape(x['source'])}: {html.escape(x['error'])}</li>" for x in failures)+"</ul>")
    return markdown,"\n".join(body)

def send_email(cfg: dict[str,Any], subject: str, text: str, html_body: str) -> None:
    d=cfg["delivery"]; user=os.getenv(d["username_env"]); password=os.getenv(d["password_env"])
    if not user or not password: raise RuntimeError("SMTP credentials are missing from environment")
    msg=email.message.EmailMessage(); msg["Subject"]=subject; msg["From"]=d["from_address"]; msg["To"]=", ".join(d["to_addresses"])
    msg.set_content(text); msg.add_alternative(html_body,subtype="html")
    with smtplib.SMTP_SSL(d["smtp_host"],int(d.get("smtp_port",465)),context=ssl.create_default_context(),timeout=30) as s:
        s.login(user,password); s.send_message(msg)

def doctor(config_path: Path) -> int:
    try:
        cfg=config_load(config_path); state=resolve_state(cfg,config_path); profile=state/cfg["profile_file"]
        issues=[]
        if sys.version_info < (3,9): issues.append("Python 3.9+ required")
        if not profile.exists(): issues.append(f"Profile not found: {profile}")
        for i,s in enumerate(cfg["sources"]):
            if s.get("type") not in ("crossref","crossref-query","openalex"): issues.append(f"sources[{i}] has unsupported type")
            if s.get("type")=="crossref" and not s.get("issn"): issues.append(f"sources[{i}] needs issn")
        print(json.dumps({"ok":not issues,"version":VERSION,"state_dir":str(state),"issues":issues},ensure_ascii=False,indent=2))
        return 0 if not issues else 2
    except Exception as e: print(json.dumps({"ok":False,"error":str(e)},ensure_ascii=False)); return 2

def row_to_paper(row: sqlite3.Row) -> Paper:
    return Paper(row["identity"],row["doi"] or "",row["title"],row["abstract"] or "",row["venue"] or "",
      row["published"] or "",row["url"] or "",json.loads(row["authors_json"] or "[]"),"database",
      row["abstract_source"] if "abstract_source" in row.keys() else "",
      bool(row["low_priority"]) if "low_priority" in row.keys() else False,
      row["low_priority_reason"] if "low_priority_reason" in row.keys() else "",
      row["publication_type_raw"] if "publication_type_raw" in row.keys() else "",
      row["publication_type_source"] if "publication_type_source" in row.keys() else "",
      "",
      row["publication_type"] if "publication_type" in row.keys() else "")

def confirmed_profile(db: sqlite3.Connection, profile_path: Path) -> sqlite3.Row:
    # Hash the exact bytes written at profile confirmation. ``read_text``
    # performs universal-newline conversion and would otherwise make a valid
    # CRLF profile appear different on the next scheduled run.
    raw=profile_path.read_bytes(); content=raw.decode("utf-8"); digest=hashlib.sha256(raw).hexdigest()
    active=db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
    if not active:
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        with db:
            db.execute("""INSERT INTO profile_versions(
              profile_hash,content,status,source,change_summary,created_at,confirmed_at
            ) VALUES(?,?,'active','legacy_import','Imported existing profile',?,?)""",(digest,content,now,now))
        active=db.execute("SELECT * FROM profile_versions WHERE status='active'").fetchone()
    if active["profile_hash"]!=digest:
        raise ValueError("research-profile.md differs from the confirmed active version; create and confirm a profile draft")
    return active

def feedback_snapshot(db: sqlite3.Connection, per_class: int=20) -> list[dict[str,Any]]:
    examples=[]
    for interest in ("interested","not_interested"):
        rows=db.execute("""SELECT f.interest,f.reason,f.updated_at,p.identity,p.title,p.abstract,p.venue
          FROM paper_feedback f JOIN papers p ON p.identity=f.identity
          WHERE f.interest=? ORDER BY f.updated_at DESC LIMIT ?""",(interest,max(1,per_class))).fetchall()
        examples.extend(dict(row) for row in rows)
    examples.sort(key=lambda x:x["updated_at"],reverse=True)
    return examples

def update_source_health(db: sqlite3.Connection, source: str, status: str, now: str, error: str="") -> None:
    prior=db.execute("SELECT * FROM source_health WHERE source=?",(source,)).fetchone()
    failures=(int(prior["consecutive_failures"]) if prior else 0) + (1 if status=="failed" else 0)
    if status!="failed": failures=0
    last_success=now if status in ("healthy","degraded") else (prior["last_success_at"] if prior else None)
    last_failure=now if status in ("degraded","failed") else (prior["last_failure_at"] if prior else None)
    db.execute("""INSERT INTO source_health VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
      status=excluded.status, consecutive_failures=excluded.consecutive_failures,
      last_success_at=excluded.last_success_at, last_failure_at=excluded.last_failure_at,
      last_error=excluded.last_error, updated_at=excluded.updated_at""",
      (source,status,failures,last_success,last_failure,error or None,now))

def enrich_missing_abstracts(papers: list[Paper], cfg: dict[str,Any], db: sqlite3.Connection | None=None) -> None:
    grouped: dict[str,list[Paper]]={}
    for paper in papers: grouped.setdefault(paper.identity,[]).append(paper)
    for group in grouped.values():
        abstract=max((paper.abstract for paper in group),key=len,default="")
        source=""
        if not abstract and db is not None:
            prior=db.execute("SELECT abstract FROM papers WHERE identity=?",(group[0].identity,)).fetchone()
            abstract=(prior[0] or "") if prior else ""
            source="existing" if abstract else ""
        if not abstract:
            abstract=openalex_abstract(group[0].doi,cfg)
            source="openalex-doi" if abstract else ""
        if abstract:
            for paper in group:
                if not paper.abstract:
                    paper.abstract=abstract
                    paper.abstract_source=source
        for paper in group:
            if not paper.low_priority:
                paper.low_priority,paper.low_priority_reason=classify_low_priority(paper.title,paper.venue)
            paper.abstract_source=abstract_source_for(paper.abstract,paper.abstract_source)

def collect_into_db(cfg: dict[str,Any], db: sqlite3.Connection, now: str, run_id: str) -> tuple[list[Paper],list[Paper],list[dict[str,str]]]:
    since=(dt.date.today()-dt.timedelta(days=int(cfg.get("lookback_days",14)))).isoformat()
    failures=[]; collected=[]
    for source in cfg["sources"]:
        papers=[]; errors=[]; attempted=0; succeeded=0
        if source.get("type") in ("crossref","crossref-query"):
            attempted += 1
            try: papers.extend(crossref_collect(source,cfg,since)); succeeded += 1
            except Exception as e: errors.append("Crossref: "+f"{type(e).__name__}: {str(e)[:200]}")
        if source.get("openalex_id") and cfg.get("collection",{}).get("openalex_fallback",True):
            attempted += 1
            try: papers.extend(openalex_collect(source,cfg,since)); succeeded += 1
            except Exception as e: errors.append("OpenAlex: "+f"{type(e).__name__}: {str(e)[:200]}")
        if papers: enrich_missing_abstracts(papers,cfg,db)
        collected.extend(papers)
        status="healthy" if succeeded==attempted else ("degraded" if succeeded else "failed")
        err="; ".join(errors)
        unique_count=len({paper.identity for paper in papers})
        db.execute("""INSERT OR REPLACE INTO source_runs(run_id,source,status,count,error,finished_at,since)
                   VALUES(?,?,?,?,?,?,?)""",
                   (run_id,source["name"],"ok" if status=="healthy" else status,unique_count,err,now,since))
        update_source_health(db,source["name"],status,now,err)
        if errors: failures.append({"source":source["name"],"status":status,"error":err})
        db.commit()
    new=[]
    for p in collected:
        with db:
            if upsert(db,p,now): new.append(p)
    unique_collected=list({paper.identity:paper for paper in collected}.values())
    unique_new=list({paper.identity:paper for paper in new}.values())
    return unique_collected,unique_new,failures

def collect_only(config_path: Path) -> int:
    """Collect the 14-day API window before official issue checks and queue freezing."""

    cfg=config_load(config_path); state=resolve_state(cfg,config_path); state.mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc).isoformat(); run_id="collect-"+now.replace(":","-")
    db=db_open(state/"papers.sqlite3")
    collected,new,failures=collect_into_db(cfg,db,now,run_id)
    required={s["name"] for s in cfg["sources"] if s.get("required",True)}
    failed_required={x["source"] for x in failures if x.get("status")=="failed" and x["source"] in required}
    status="partial" if failures else "succeeded"
    if required and failed_required==required: status="failed"
    summary={
        "run_id":run_id,
        "started_at":now,
        "collected":len(collected),
        "new":len(new),
        "new_identities":[paper.identity for paper in new],
        "source_failures":failures,
        "next_step":"Run official two-issue imports, then agent-export --no-collect --batch-run " + run_id,
    }
    with db:
        db.execute("""INSERT OR REPLACE INTO pipeline_runs(
          run_id,kind,status,started_at,finished_at,collected_count,candidate_count,relevant_count,error_summary,details_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (run_id,"collection",status,now,dt.datetime.now(dt.timezone.utc).isoformat(),len(collected),0,0,
         "; ".join(x["error"] for x in failures) or None,json.dumps(summary,ensure_ascii=False)))
        for paper in collected:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'collected')",(run_id,paper.identity))
        for paper in new:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'new')",(run_id,paper.identity))
    db.close()
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if required and failed_required==required else 0


def agent_export(config_path: Path, rescreen: bool=False, no_collect: bool=False,
                 batch_run: str | None=None) -> int:
    cfg=config_load(config_path); state=resolve_state(cfg,config_path); state.mkdir(parents=True,exist_ok=True)
    profile_path=state/cfg["profile_file"]
    now=dt.datetime.now(dt.timezone.utc).isoformat(); run_id=now.replace(":","-"); db=db_open(state/"papers.sqlite3")
    active=confirmed_profile(db,profile_path); phash=active["profile_hash"]
    with db:
        stale=[row[0] for row in db.execute("SELECT run_id FROM agent_jobs WHERE status='exported'")]
        db.execute("UPDATE agent_jobs SET status='abandoned' WHERE status='exported'")
        if stale:
            marks=",".join("?" for _ in stale)
            db.execute(f"UPDATE pipeline_runs SET status='abandoned',finished_at=? WHERE run_id IN ({marks})",
                       (now,*stale))
    enrichment: dict[str,Any] | None = None
    if batch_run and not no_collect:
        raise ValueError("--batch-run requires --no-collect")
    if no_collect:
        collected,new,failures=[],[],[]
        if batch_run:
            batch=db.execute("SELECT * FROM pipeline_runs WHERE run_id=? AND kind='collection'",(batch_run,)).fetchone()
            if not batch:
                raise ValueError(f"Unknown collection batch: {batch_run}")
            details=json.loads(batch["details_json"] or "{}")
            failures=list(details.get("source_failures") or [])
            previous=db.execute(
                """SELECT imported_at FROM agent_jobs WHERE status='imported' AND imported_at<?
                ORDER BY imported_at DESC LIMIT 1""", (batch["started_at"],)
            ).fetchone()
            # Use the previous completed judgment as the daily boundary.  This
            # keeps genuinely new papers visible even if collection is retried
            # before the one authoritative export is frozen.
            if previous and previous["imported_at"]:
                fresh=db.execute("SELECT * FROM papers WHERE first_seen>? ORDER BY first_seen",(previous["imported_at"],)).fetchall()
            else:
                fresh=db.execute("SELECT * FROM papers WHERE first_seen>=? ORDER BY first_seen",(batch["started_at"],)).fetchall()
            collected=[row_to_paper(row) for row in fresh]
            new=list(collected)
    else:
        collected,new,failures=collect_into_db(cfg,db,now,run_id)
        # Complete traceable metadata before freezing the one authoritative
        # queue so this run retains its `new` identities for Today's Radar.
        enrichment=run_enrichment(state/"papers.sqlite3",cfg,limit=500)
    if rescreen:
        rows=db.execute("SELECT * FROM papers WHERE eligibility_status='eligible' ORDER BY first_seen").fetchall()
    else:
        rows=db.execute("""SELECT p.* FROM papers p WHERE (
          p.needs_rescreen=1 OR NOT EXISTS(
          SELECT 1 FROM screenings s WHERE s.identity=p.identity AND s.profile_hash=? AND s.provider='codex-agent')
          ) AND p.eligibility_status='eligible' ORDER BY p.first_seen""",(phash,)).fetchall()
    papers=[asdict(row_to_paper(row)) for row in rows]
    queue_dir=state/"agent_queue"; queue_dir.mkdir(exist_ok=True)
    queue_path=queue_dir/f"{run_id.replace('+','_')}.json"
    examples=feedback_snapshot(db,int(cfg.get("feedback_examples_per_class",20)))
    payload={"schema_version":2,"run_id":run_id,"profile_hash":phash,"profile_version_id":active["id"],
             "profile_confirmed_at":active["confirmed_at"],"profile_path":str(profile_path),
             "feedback_examples":examples,"threshold":float(cfg.get("relevance_threshold",0.70)),
             "papers":papers,"source_failures":failures,"collection_run_id":batch_run}
    queue_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    summary={"run_id":run_id,"collected":len(collected),"new":len(new),"candidates":len(papers),
             "queue_path":str(queue_path),"profile_path":str(profile_path),"source_failures":failures,
             "collection_run_id":batch_run}
    if enrichment is not None: summary["enrichment"]=enrichment
    run_status="partial" if failures else "succeeded"
    if failures and all(x.get("status")=="failed" for x in failures): run_status="failed"
    profile_row=db.execute("SELECT id FROM profile_versions WHERE profile_hash=? AND status='active'",(phash,)).fetchone()
    with db:
        db.execute("""INSERT OR REPLACE INTO pipeline_runs(
          run_id,kind,status,profile_version_id,started_at,finished_at,collected_count,candidate_count,relevant_count,error_summary,details_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id,"agent-export",run_status,profile_row[0] if profile_row else None,now,dt.datetime.now(dt.timezone.utc).isoformat(),len(collected),len(papers),0,
         "; ".join(x["error"] for x in failures) or None,json.dumps(summary,ensure_ascii=False)))
        db.execute("""INSERT OR REPLACE INTO agent_jobs(
          run_id,profile_hash,status,queue_path,results_path,exported_count,imported_count,created_at,imported_at,
          profile_version_id,feedback_snapshot_json
        ) VALUES(?,?,'exported',?,NULL,?,0,?,NULL,?,?)""",
        (run_id,phash,str(queue_path),len(papers),now,active["id"],json.dumps(examples,ensure_ascii=False)))
        for paper in collected:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'collected')",(run_id,paper.identity))
        for paper in new:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'new')",(run_id,paper.identity))
        for paper in papers:
            db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'candidate')",(run_id,paper["identity"]))
    required={s["name"] for s in cfg["sources"] if s.get("required",True)}
    failed_required={x["source"] for x in failures if x.get("status")=="failed" and x["source"] in required}
    db.close()
    print(json.dumps(summary,ensure_ascii=False,indent=2)); return 1 if required and failed_required==required else 0

def agent_import(config_path: Path, results_path: Path) -> int:
    cfg=config_load(config_path); state=resolve_state(cfg,config_path); db=db_open(state/"papers.sqlite3")
    active=confirmed_profile(db,state/cfg["profile_file"]); phash=active["profile_hash"]
    data=json.loads(results_path.read_text(encoding="utf-8"))
    if data.get("profile_hash") != phash: raise ValueError("Result profile_hash does not match the current research profile")
    results=data.get("results");
    if not isinstance(results,list): raise ValueError("results must be a list")
    now=dt.datetime.now(dt.timezone.utc).isoformat(); selected=[]; imported=0
    run_id=data.get("run_id") or now.replace(":","-")
    job=db.execute("SELECT * FROM agent_jobs WHERE run_id=?",(run_id,)).fetchone()
    if not job: raise ValueError("Results do not belong to an exported agent job")
    if job["status"]!="exported": raise ValueError(f"Agent job is not importable: {job['status']}")
    if job["profile_hash"]!=phash: raise ValueError("Agent job profile does not match the active profile")
    identities=[str(item.get("identity","")) for item in results]
    if len(identities)!=len(set(identities)): raise ValueError("results contain duplicate paper identities")
    queue_path=Path(job["queue_path"])
    if not queue_path.exists(): raise FileNotFoundError(f"Agent queue not found: {queue_path}")
    queue=json.loads(queue_path.read_text(encoding="utf-8"))
    if queue.get("run_id")!=run_id or queue.get("profile_hash")!=phash:
        raise ValueError("Agent queue metadata does not match the result job")
    if data.get("source_failures",[])!=queue.get("source_failures",[]):
        raise ValueError("Result source_failures do not match the exported queue")
    expected={paper["identity"] for paper in queue.get("papers",[])}
    received=set(identities)
    if received != expected:
        missing=sorted(expected-received); extra=sorted(received-expected)
        raise ValueError(f"Results must cover the complete queue (missing={len(missing)}, extra={len(extra)})")
    threshold=float(cfg.get("relevance_threshold",0.70))
    validated=[]
    for item in results:
        identity_value=item.get("identity",""); row=db.execute("SELECT * FROM papers WHERE identity=?",(identity_value,)).fetchone()
        if not row: raise ValueError(f"Unknown paper identity: {identity_value}")
        result=extract_json(json.dumps(item,ensure_ascii=False)); result["relevant"]=result["score"]>=threshold
        if not (row["abstract"] or "").strip():
            result["confidence"]=min(result["confidence"],0.5)
        validated.append((identity_value,row,result))
        if result["relevant"]: selected.append((row_to_paper(row),result))
    digest_dir=state/"digests"; digest_dir.mkdir(exist_ok=True)
    markdown,_=render_digest(selected,data.get("source_failures",[]),run_id)
    digest_path=digest_dir/f"{str(run_id).replace('+','_')}-agent.md"; digest_path.write_text(markdown,encoding="utf-8")
    snapshot=job["feedback_snapshot_json"]; version_id=job["profile_version_id"]
    model=str(data.get("model","")).strip()
    if not model: raise ValueError("results must identify the actual model")
    with db:
        for identity_value,_,result in validated:
            db.execute("""INSERT OR REPLACE INTO screenings(
              identity,profile_hash,provider,model,relevant,score,reasons,themes_json,confidence,screened_at,
              profile_version_id,feedback_snapshot_json,run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (identity_value,phash,"codex-agent",model,int(result["relevant"]),result["score"],
               result["reasons"],json.dumps(result["matched_themes"],ensure_ascii=False),result["confidence"],now,
               version_id,snapshot,run_id))
            db.execute("UPDATE papers SET needs_rescreen=0 WHERE identity=?",(identity_value,))
            if result["relevant"]:
                db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'selected')",(run_id,identity_value))
                if db.execute("SELECT 1 FROM run_papers WHERE run_id=? AND identity=? AND role='new'",(run_id,identity_value)).fetchone():
                    db.execute("INSERT OR IGNORE INTO run_papers(run_id,identity,role) VALUES(?,?,'selected_new')",(run_id,identity_value))
        imported=len(validated)
        db.execute("""UPDATE pipeline_runs SET status='succeeded',finished_at=?,relevant_count=?,details_json=?
          WHERE run_id=?""",(now,len(selected),json.dumps({"results_path":str(results_path),"digest_path":str(digest_path)},ensure_ascii=False),run_id))
        db.execute("""UPDATE agent_jobs SET status='imported',results_path=?,imported_count=?,imported_at=?
          WHERE run_id=?""",(str(results_path),imported,now,run_id))
    db.close()
    print(json.dumps({"run_id":run_id,"imported":imported,"relevant":len(selected),"digest_path":str(digest_path)},ensure_ascii=False,indent=2))
    return 0

def enrich_abstracts(config_path: Path, limit: int=100) -> int:
    """Run the traceable multi-provider metadata enrichment pipeline."""
    cfg=config_load(config_path); state=resolve_state(cfg,config_path)
    result=run_enrichment(state/"papers.sqlite3",cfg,limit=limit)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--version",action="version",version=VERSION)
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("doctor","collect-only","agent-export","agent-import","enrich-abstracts"):
        p=sub.add_parser(name); p.add_argument("--config",required=True,type=Path)
        if name=="agent-export":
            p.add_argument("--rescreen",action="store_true",help="export all stored papers, including previously judged papers")
            p.add_argument("--no-collect",action="store_true",help="export from the database without network collection")
            p.add_argument("--batch-run",help="collection run to carry into this frozen queue")
        elif name=="agent-import":
            p.add_argument("--results",required=True,type=Path)
        elif name=="enrich-abstracts":
            p.add_argument("--limit",type=int,default=100)
    a=parser.parse_args()
    try:
        if a.command=="doctor": return doctor(a.config)
        if a.command=="collect-only": return collect_only(a.config)
        if a.command=="agent-export": return agent_export(a.config,a.rescreen,a.no_collect,a.batch_run)
        if a.command=="agent-import": return agent_import(a.config,a.results)
        if a.command=="enrich-abstracts": return enrich_abstracts(a.config,a.limit)
        raise ValueError("Unsupported command")
    except Exception as e: print(json.dumps({"ok":False,"error":f"{type(e).__name__}: {e}"},ensure_ascii=False),file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
