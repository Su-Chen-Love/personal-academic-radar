"""Verified publisher issue plans and strict official-page imports."""

from __future__ import annotations

import datetime as dt
import concurrent.futures
import html
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .governance import publication_decision
from .storage import connect, upgrade_database, utc_now


OFFICIAL_SOURCE_REGISTRY: dict[str, dict[str, str]] = {
    "1044-7318": {
        "provider": "Taylor & Francis",
        "issues_url": "https://www.tandfonline.com/loi/hihc20",
        "feed_url": "https://www.tandfonline.com/action/showFeed?type=etoc&feed=rss&jc=hihc20",
    },
    "1071-5819": {
        "provider": "Elsevier ScienceDirect",
        "issues_url": "https://www.sciencedirect.com/journal/international-journal-of-human-computer-studies/issues",
        "feed_url": "https://rss.sciencedirect.com/publication/science/10715819",
    },
    "2168-2291": {
        "provider": "IEEE Xplore",
        "issues_url": "https://ieeexplore.ieee.org/xpl/RecentIssue.jsp?punumber=6221037",
    },
    "0169-8141": {
        "provider": "Elsevier ScienceDirect",
        "issues_url": "https://www.sciencedirect.com/journal/international-journal-of-industrial-ergonomics/issues",
        "feed_url": "https://rss.sciencedirect.com/publication/science/01698141",
    },
    "0747-5632": {
        "provider": "Elsevier ScienceDirect",
        "issues_url": "https://www.sciencedirect.com/journal/computers-in-human-behavior/issues",
        "feed_url": "https://rss.sciencedirect.com/publication/science/07475632",
    },
    "2397-3374": {
        "provider": "Springer Nature",
        "issues_url": "https://www.nature.com/nathumbehav/browse-issues",
        "feed_url": "https://www.nature.com/nathumbehav.rss",
    },
    "1073-0516": {
        "provider": "ACM Digital Library",
        "issues_url": "https://dl.acm.org/loi/tochi",
    },
    "0025-1909": {
        "provider": "INFORMS PubsOnline",
        "issues_url": "https://pubsonline.informs.org/loi/mnsc",
        "feed_url": "https://pubsonline.informs.org/action/showFeed?type=etoc&feed=rss&jc=mnsc",
    },
    "0018-7208": {
        "provider": "SAGE Journals",
        "issues_url": "https://journals.sagepub.com/loi/hfs",
    },
    "0014-0139": {
        "provider": "Taylor & Francis",
        "issues_url": "https://www.tandfonline.com/loi/terg20",
        "feed_url": "https://www.tandfonline.com/action/showFeed?type=etoc&feed=rss&jc=terg20",
    },
    "0737-0024": {
        "provider": "Taylor & Francis",
        "issues_url": "https://www.tandfonline.com/loi/hhci20",
        "feed_url": "https://www.tandfonline.com/action/showFeed?type=etoc&feed=rss&jc=hhci20",
    },
}

SECONDARY_ABSTRACT_EVIDENCE_HOSTS = {
    "www.researchgate.net",
    "eurekamag.com",
    "hrinteraction.com",
    "erglab.bjtu.edu.cn",
}


def _clean(value: Any) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _title_matches(left: str, right: str) -> bool:
    a, b = _normalized_title(left), _normalized_title(right)
    return bool(a and b and (a == b or (min(len(a), len(b)) > 24 and (a in b or b in a))))


def _normalized_issn(value: str) -> str:
    compact = re.sub(r"[^0-9Xx]", "", value or "").upper()
    return compact[:4] + "-" + compact[4:] if len(compact) == 8 else ""


def resolve_official_source(source: dict[str, Any]) -> dict[str, Any] | None:
    """Return a persisted override or a verified built-in publisher mapping."""

    issues_url = str(source.get("official_issues_url") or "").strip()
    if issues_url:
        return {
            "provider": str(source.get("official_provider") or "Official publisher"),
            "issues_url": issues_url,
            "feed_url": str(source.get("official_feed_url") or ""),
            "mode": "browser_official",
            "status": "verified",
        }
    spec = OFFICIAL_SOURCE_REGISTRY.get(_normalized_issn(str(source.get("issn") or "")))
    if not spec:
        return None
    return {**spec, "mode": "browser_official", "status": "verified"}


def configure_official_source(source: dict[str, Any]) -> dict[str, Any]:
    """Persist a verified mapping when known, otherwise mark the API bridge honestly."""

    output = dict(source)
    spec = resolve_official_source(output)
    if spec:
        output.update({
            "official_status": "verified",
            "official_provider": spec["provider"],
            "official_issues_url": spec["issues_url"],
        })
        if spec.get("feed_url"):
            output["official_feed_url"] = spec["feed_url"]
    else:
        output["official_status"] = "api_fallback"
    return output


def build_official_plan(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Describe the two-issue browser work without guessing publisher URLs."""

    upgrade_database(db_path)
    db = connect(db_path)
    try:
        planned: list[dict[str, Any]] = []
        fallback: list[dict[str, str]] = []
        as_of_date = dt.date.today().isoformat()
        for source in config.get("sources", []):
            if source.get("type") != "crossref" or not source.get("issn"):
                fallback.append({
                    "source_name": str(source.get("name") or ""),
                    "reason": "该来源不是期刊卷期来源，继续使用 14 天 API 采集",
                })
                continue
            spec = resolve_official_source(source)
            if not spec:
                fallback.append({
                    "source_name": str(source.get("name") or ""),
                    "reason": "尚未验证官方卷期目录，先使用 14 天 API 采集并等待适配",
                })
                continue
            checked = [dict(row) for row in db.execute(
                """SELECT issue_key,issue_url,status,article_count,imported_count,checked_at
                FROM official_issue_checks WHERE source_name=? AND status='succeeded'
                ORDER BY checked_at DESC LIMIT 12""",
                (source["name"],),
            )]
            planned.append({
                "source_name": source["name"],
                "issn": source["issn"],
                **spec,
                "issue_limit": 2,
                "as_of_date": as_of_date,
                "checked_issues": checked,
                "instructions": (
                    f"核验官方卷期目录或出版商提交的印刷卷期元数据，选择出版日期不晚于 {as_of_date} 的最新两期；"
                    "未来卷期必须跳过。摘要优先取官网原文或出版商提交的 Crossref 完整元数据；仍缺失时仅可用 DOI 完全一致的"
                    " OpenAlex 完整摘要并保留来源。已成功核验的相同 issue_key 可跳过。"
                ),
            })
        return {
            "schema_version": 1,
            "created_at": utc_now(),
            "as_of_date": as_of_date,
            "issue_limit": 2,
            "sources": planned,
            "api_fallback": fallback,
            "rules": [
                "接受官网原文、出版商提交的 Crossref 完整摘要，或 DOI 完全一致的 OpenAlex 完整摘要；必须保留真实 provenance。",
                f"每个期刊只核验出版日期不晚于 {as_of_date} 的最新两期；未来卷期不计入，并用 DOI 去重。",
                "禁止把搜索片段、Highlights、总结、改写或猜测写成摘要；不可得项必须留空并记录可追溯原因。",
                "Editorial Board、勘误、公告等仍可记录，但会由出版类型治理排除。",
            ],
        }
    finally:
        db.close()


def write_official_plan(db_path: Path, config: dict[str, Any], output: Path) -> dict[str, Any]:
    payload = build_official_plan(db_path, config)
    path = output.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output": str(path), "sources": len(payload["sources"]), "fallback": len(payload["api_fallback"])}


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 PersonalAcademicRadar/0.8"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", "replace")


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PersonalAcademicRadar/0.8 (official issue verifier)"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        value = json.loads(response.read().decode("utf-8", "replace"))
    if not isinstance(value, dict):
        raise ValueError("元数据响应不是对象")
    return value


def _date_parts(value: Any) -> str:
    try:
        parts = value["date-parts"][0]
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return dt.date(year, month, day).isoformat()
    except (KeyError, IndexError, TypeError, ValueError):
        return ""


def _openalex_abstract(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    positions: dict[int, str] = {}
    for word, indexes in value.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int) and index >= 0:
                positions[index] = str(word)
    return " ".join(positions[index] for index in sorted(positions))


def _metadata_issue_url(spec: dict[str, Any], volume: str, issue: str) -> str:
    provider = spec["provider"]
    if provider == "Taylor & Francis":
        code = urllib.parse.urlparse(spec["issues_url"]).path.rstrip("/").split("/")[-1]
        return f"https://www.tandfonline.com/toc/{code}/{volume}/{issue}"
    if provider == "ACM Digital Library":
        return f"https://dl.acm.org/toc/tochi/{volume}/{issue}"
    if provider == "SAGE Journals":
        return f"https://journals.sagepub.com/toc/hfs/{volume}/{issue}"
    if provider == "IEEE Xplore":
        separator = "&" if "?" in spec["issues_url"] else "?"
        return f'{spec["issues_url"]}{separator}volume={urllib.parse.quote(volume)}&issue={urllib.parse.quote(issue)}'
    raise ValueError(f"{provider} 尚未配置元数据卷期 URL 规则")


def _metadata_article_url(spec: dict[str, Any], doi: str) -> str:
    provider = spec["provider"]
    if provider == "Taylor & Francis":
        return f"https://www.tandfonline.com/doi/full/{doi}"
    if provider == "ACM Digital Library":
        return f"https://dl.acm.org/doi/{doi}"
    if provider == "SAGE Journals":
        return f"https://journals.sagepub.com/doi/{doi}"
    if provider == "IEEE Xplore":
        number = doi.rsplit(".", 1)[-1]
        return f"https://ieeexplore.ieee.org/document/{number}"
    raise ValueError(f"{provider} 尚未配置论文 URL 规则")


def _metadata_publication_type(title: str) -> str:
    normalized = title.lower().strip()
    if re.match(r"^(editorial\s+board|editorial)\b", normalized):
        return "editorial-board" if normalized.startswith("editorial board") else "editorial"
    if re.match(r"^(correction|erratum|corrigendum|retraction)\b", normalized):
        return "correction"
    if re.match(r"^(book review|review of)\b", normalized):
        return "book-review"
    if (
        re.match(r"^(call for papers|table of contents)\b", normalized)
        or normalized in {"connect. support. inspire.", "present a world of opportunity"}
        or normalized.startswith("introducing ieee collabratec")
        or normalized.startswith("techrxiv:")
        or "society information" in normalized
        or "information for authors" in normalized
    ):
        return "paratext"
    return "journal-article"


def _collect_print_metadata_source(
    source: dict[str, Any], spec: dict[str, Any], as_of_date: str,
) -> dict[str, Any]:
    """Use publisher-deposited print metadata to select issues, then fill exact-DOI abstracts."""

    issn = _normalized_issn(str(source.get("issn") or ""))
    if not issn:
        raise ValueError("期刊 ISSN 无效")
    year = dt.date.fromisoformat(as_of_date).year
    query = urllib.parse.urlencode({
        "filter": (
            f"from-print-pub-date:{year}-01-01,until-print-pub-date:{as_of_date},"
            "type:journal-article"
        ),
        "rows": "1000",
        "select": "DOI,title,abstract,published-print,volume,issue,author,URL,type,publisher,resource",
    })
    crossref_url = f"https://api.crossref.org/journals/{issn}/works?{query}"
    response = _fetch_json(crossref_url).get("message")
    records = response.get("items") if isinstance(response, dict) else None
    if not isinstance(records, list):
        raise ValueError("Crossref 没有返回出版商提交的论文记录")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    group_dates: dict[tuple[str, str], str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        volume = _clean(record.get("volume"))
        issue = _clean(record.get("issue"))
        published = _date_parts(record.get("published-print"))
        if not volume or not issue or not published or published > as_of_date:
            continue
        key = (volume, issue)
        grouped[key].append(record)
        group_dates[key] = max(group_dates.get(key, ""), published)
    selected = sorted(grouped, key=lambda key: (group_dates[key], key), reverse=True)[:2]
    if len(selected) != 2:
        raise ValueError("出版商提交的印刷卷期元数据没有提供截至运行日期的最近两期")

    def enrich(record: dict[str, Any]) -> dict[str, Any]:
        doi = _clean(record.get("DOI")).lower()
        title_values = record.get("title")
        title = _clean(title_values[0] if isinstance(title_values, list) and title_values else title_values)
        abstract = _clean(record.get("abstract"))
        evidence_url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
        evidence_type = "publisher_deposited_metadata"
        if not abstract and doi:
            openalex_url = (
                "https://api.openalex.org/works/https://doi.org/" +
                urllib.parse.quote(doi, safe="")
            )
            openalex = _fetch_json(openalex_url)
            expected_doi = "https://doi.org/" + doi
            if str(openalex.get("doi") or "").lower() == expected_doi:
                candidate = _clean(_openalex_abstract(openalex.get("abstract_inverted_index")))
                if candidate:
                    abstract = candidate
                    evidence_url = openalex_url
                    evidence_type = "scholarly_metadata"
        authors = []
        for author in record.get("author") or []:
            if isinstance(author, dict):
                name = _clean(" ".join((str(author.get("given") or ""), str(author.get("family") or ""))))
                if name:
                    authors.append(name)
        article_url = _metadata_article_url(spec, doi)
        resource = record.get("resource")
        if isinstance(resource, dict) and isinstance(resource.get("primary"), dict):
            candidate_url = str(resource["primary"].get("URL") or "").strip()
            expected_host = urllib.parse.urlparse(spec["issues_url"]).hostname or ""
            if urllib.parse.urlparse(candidate_url).hostname == expected_host:
                article_url = candidate_url
        return {
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": _date_parts(record.get("published-print")),
            "source_url": article_url,
            "publication_type_raw": _metadata_publication_type(title),
            **({
                "abstract_evidence_url": evidence_url,
                "abstract_evidence_type": evidence_type,
            } if abstract else {}),
        }

    output_issues: list[dict[str, Any]] = []
    for volume, issue in selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            papers = list(executor.map(enrich, grouped[(volume, issue)]))
        output_issues.append({
            "issue_key": f"volume-{volume}-issue-{issue}",
            "issue_url": _metadata_issue_url(spec, volume, issue),
            "papers": papers,
        })
    return {"source_name": source["name"], "issues": output_issues}


class _NatureIssueIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pending_url = ""
        self.issues: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "a" and values.get("itemprop") == "url":
            candidate = values.get("content") or values.get("href") or ""
            if "/issues/" in candidate:
                self.pending_url = urllib.parse.urljoin("https://www.nature.com", candidate)
        elif values.get("itemprop") == "datePublished" and self.pending_url:
            published = values.get("content") or ""
            self.issues.append((self.pending_url, published))
            self.pending_url = ""


class _ArticleLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if re.fullmatch(r"/articles/[A-Za-z0-9.-]+", href) and href not in self.links:
            self.links.append(href)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = defaultdict(list)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = {key: value or "" for key, value in attrs}
        name = (values.get("name") or values.get("property") or "").lower()
        if name:
            self.values[name].append(html.unescape(values.get("content") or "").strip())

    def first(self, name: str) -> str:
        return (self.values.get(name.lower()) or [""])[0]


def _nature_publication_type(article_type: str, dc_type: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (article_type or dc_type).lower()).strip("-")
    return {
        "article": "research-article",
        "registered-report": "research-article",
        "review-article": "review-article",
        "perspective": "review-article",
        "comment": "comment",
        "world-view": "comment",
        "research-briefing": "research-briefing",
        "correspondence": "correspondence",
        "news-views": "news-and-views",
        "editorial": "editorial",
    }.get(normalized, normalized or "journal-article")


def _nature_date(value: str) -> str:
    parts = [part for part in re.split(r"[/\-]", value or "") if part]
    if len(parts) >= 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    if len(parts) == 2:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-01"
    return ""


def _collect_nature_source(source: dict[str, Any], spec: dict[str, Any], as_of_date: str) -> dict[str, Any]:
    index = _NatureIssueIndexParser()
    index.feed(_fetch_text(spec["issues_url"]))
    cutoff = dt.date.fromisoformat(as_of_date)
    eligible_issues: list[tuple[str, dt.date]] = []
    for issue_url, display_date in index.issues:
        try:
            month = dt.datetime.strptime(display_date, "%B %Y").date().replace(day=1)
        except ValueError:
            continue
        if month <= cutoff:
            eligible_issues.append((issue_url, month))
    eligible_issues.sort(key=lambda item: item[1], reverse=True)
    selected = eligible_issues[:2]
    if len(selected) != 2:
        raise ValueError("官网卷期目录没有提供截至运行日期的最近两期")

    output_issues: list[dict[str, Any]] = []
    for issue_url, _ in selected:
        volume_match = re.search(r"/volumes/(\d+)/issues/(\d+)", issue_url)
        if not volume_match:
            raise ValueError("Nature 卷期 URL 缺少卷号或期号")
        links = _ArticleLinkParser()
        links.feed(_fetch_text(issue_url))
        if not links.links:
            raise ValueError(f"官网卷期没有发现论文链接：{issue_url}")

        def fetch_article(path: str) -> dict[str, Any]:
            article_url = urllib.parse.urljoin("https://www.nature.com", path)
            metadata = _MetaParser()
            metadata.feed(_fetch_text(article_url))
            published = _nature_date(
                metadata.first("citation_online_date") or metadata.first("citation_publication_date")
            )
            return {
                "doi": metadata.first("citation_doi"),
                "title": metadata.first("citation_title"),
                "abstract": metadata.first("dc.description"),
                "authors": metadata.values.get("citation_author", []),
                "published": published,
                "source_url": article_url,
                "publication_type_raw": _nature_publication_type(
                    metadata.first("citation_article_type"), metadata.first("dc.type")
                ),
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            papers = list(executor.map(fetch_article, links.links))
        output_issues.append({
            "issue_key": f"volume-{volume_match.group(1)}-issue-{volume_match.group(2)}",
            "issue_url": issue_url,
            "papers": papers,
        })
    return {"source_name": source["name"], "issues": output_issues}


def collect_supported_official(
    db_path: Path,
    config: dict[str, Any],
    output: Path,
    source_name: str = "",
) -> dict[str, Any]:
    """Collect strict official-page evidence for publishers with deterministic adapters."""

    plan = build_official_plan(db_path, config)
    configured = {item["name"]: item for item in config.get("sources", [])}
    collected: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    matched = False
    for item in plan["sources"]:
        if source_name and item["source_name"] != source_name:
            continue
        matched = True
        source = configured[item["source_name"]]
        if item["provider"] not in {
            "Springer Nature", "Taylor & Francis", "IEEE Xplore",
            "ACM Digital Library", "SAGE Journals",
        }:
            unsupported.append({
                "source_name": item["source_name"],
                "reason": "该出版商仍需浏览器逐篇核验",
            })
            continue
        try:
            if item["provider"] == "Springer Nature":
                collected.append(_collect_nature_source(source, item, plan["as_of_date"]))
            else:
                collected.append(_collect_print_metadata_source(source, item, plan["as_of_date"]))
        except Exception as exc:
            failures.append({"source_name": item["source_name"], "reason": str(exc)})
    if source_name and not matched:
        raise ValueError("指定来源不在官网核验计划中")
    payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "as_of_date": plan["as_of_date"],
        "sources": collected,
        "unsupported": unsupported,
        "failures": failures,
    }
    path = output.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "output": str(path),
        "sources": len(collected),
        "issues": sum(len(item["issues"]) for item in collected),
        "papers": sum(len(issue["papers"]) for item in collected for issue in item["issues"]),
        "unsupported": unsupported,
        "failures": failures,
    }


def official_status(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable per-source view of successful and failed issue checks."""

    upgrade_database(db_path)
    db = connect(db_path)
    try:
        sources: list[dict[str, Any]] = []
        for source in config.get("sources", []):
            spec = resolve_official_source(source)
            if not spec:
                sources.append({
                    "source_name": str(source.get("name") or ""),
                    "mode": "api_fallback",
                    "provider": "",
                    "issues_url": "",
                    "succeeded_issues": 0,
                    "failed_issues": 0,
                    "latest_check": None,
                })
                continue
            rows = [dict(row) for row in db.execute(
                """SELECT issue_key,issue_url,status,article_count,imported_count,detail,checked_at
                FROM official_issue_checks WHERE source_name=? ORDER BY checked_at DESC""",
                (source["name"],),
            )]
            sources.append({
                "source_name": source["name"],
                "mode": "browser_official",
                "provider": spec["provider"],
                "issues_url": spec["issues_url"],
                "succeeded_issues": sum(item["status"] == "succeeded" for item in rows),
                "failed_issues": sum(item["status"] == "failed" for item in rows),
                "latest_check": rows[0] if rows else None,
            })
        return {
            "as_of_date": dt.date.today().isoformat(),
            "sources": sources,
            "counts": {
                "official": sum(item["mode"] == "browser_official" for item in sources),
                "api_fallback": sum(item["mode"] == "api_fallback" for item in sources),
                "with_success": sum(item["succeeded_issues"] > 0 for item in sources),
                "with_failure": sum(item["failed_issues"] > 0 for item in sources),
            },
        }
    finally:
        db.close()


def record_official_failure(
    db_path: Path,
    config: dict[str, Any],
    source_name: str,
    issue_key: str,
    issue_url: str,
    detail: str,
) -> dict[str, Any]:
    """Persist a recoverable official-page failure without touching paper data."""

    upgrade_database(db_path)
    source = next((item for item in config.get("sources", []) if item.get("name") == source_name), None)
    spec = resolve_official_source(source or {}) if source else None
    clean_key = _clean(issue_key)
    clean_detail = _clean(detail)
    expected_host = urllib.parse.urlparse(spec["issues_url"]).hostname if spec else ""
    actual_host = urllib.parse.urlparse(issue_url).hostname or ""
    if not source or not spec:
        raise ValueError("来源未配置或尚未验证官网目录")
    if not clean_key or not clean_detail:
        raise ValueError("失败记录必须包含卷期标识和具体原因")
    if actual_host != expected_host:
        raise ValueError("失败记录的卷期 URL 不是该来源已验证官网")
    now = utc_now()
    db = connect(db_path)
    try:
        with db:
            db.execute(
                """INSERT INTO official_issue_checks(
                source_name,issue_key,issue_url,status,article_count,imported_count,detail,checked_at
                ) VALUES(?,?,?,'failed',0,0,?,?)
                ON CONFLICT(source_name,issue_key) DO UPDATE SET
                issue_url=excluded.issue_url,status='failed',article_count=0,imported_count=0,
                detail=excluded.detail,checked_at=excluded.checked_at""",
                (source_name, clean_key, issue_url, clean_detail, now),
            )
        return {
            "source_name": source_name,
            "issue_key": clean_key,
            "status": "failed",
            "detail": clean_detail,
            "checked_at": now,
        }
    finally:
        db.close()


def _load_results(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8-sig"))
    if isinstance(value, dict):
        value = value.get("sources", value.get("results"))
    if not isinstance(value, list):
        raise ValueError("官网结果必须是数组，或包含 sources/results 数组")
    return [dict(item) for item in value]


def preview_official_import(
    db_path: Path, config: dict[str, Any], path: Path,
) -> dict[str, Any]:
    upgrade_database(db_path)
    configured = {str(item.get("name")): item for item in config.get("sources", [])}
    db = connect(db_path)
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    seen_dois: set[str] = set()
    try:
        for source_index, result in enumerate(_load_results(path), 1):
            source_name = str(result.get("source_name") or "").strip()
            source = configured.get(source_name)
            spec = resolve_official_source(source or {}) if source else None
            if not source or not spec:
                errors.append({"source": source_name, "reason": "来源未配置或尚未验证官网目录"})
                continue
            expected_host = urllib.parse.urlparse(spec["issues_url"]).hostname or ""
            issues = result.get("issues")
            if not isinstance(issues, list) or len(issues) > 2:
                errors.append({"source": source_name, "reason": "每个来源必须提供不超过两期的 issues 数组"})
                continue
            for issue_index, issue in enumerate(issues, 1):
                issue_key = _clean(issue.get("issue_key"))
                issue_url = str(issue.get("issue_url") or "").strip()
                issue_host = urllib.parse.urlparse(issue_url).hostname or ""
                if not issue_key or issue_host != expected_host:
                    errors.append({"source": source_name, "issue": issue_key, "reason": "卷期标识为空或卷期 URL 不是已验证官网"})
                    continue
                prior = db.execute(
                    "SELECT status FROM official_issue_checks WHERE source_name=? AND issue_key=?",
                    (source_name, issue_key),
                ).fetchone()
                if prior and prior["status"] == "succeeded":
                    skipped.append({"source": source_name, "issue": issue_key, "reason": "该卷期已经成功核验"})
                    continue
                papers = issue.get("papers")
                if not isinstance(papers, list):
                    errors.append({"source": source_name, "issue": issue_key, "reason": "papers 必须是数组"})
                    continue
                accepted_papers: list[dict[str, Any]] = []
                issue_failed = False
                for paper_index, item in enumerate(papers, 1):
                    doi = str(item.get("doi") or "").strip().lower()
                    doi = re.sub(r"^(?:https?://doi\.org/|doi:\s*)", "", doi)
                    title = _clean(item.get("title"))
                    article_url = str(item.get("source_url") or "").strip()
                    article_host = urllib.parse.urlparse(article_url).hostname or ""
                    abstract = _clean(item.get("abstract"))
                    abstract_evidence_url = str(item.get("abstract_evidence_url") or "").strip()
                    abstract_evidence_type = _clean(item.get("abstract_evidence_type"))
                    abstract_evidence_host = urllib.parse.urlparse(abstract_evidence_url).hostname or ""
                    abstract_unavailable = item.get("abstract_unavailable_official") is True
                    abstract_unavailable_traceable = item.get("abstract_unavailable_traceable") is True
                    abstract_failure_reason = _clean(item.get("abstract_failure_reason"))
                    raw_type = str(item.get("publication_type_raw") or "journal-article")
                    decision = publication_decision(title, source_name, raw_type, "publisher-official", "journal")
                    reason = ""
                    if not doi or not title:
                        reason = "DOI 或标题为空"
                    elif doi in seen_dois:
                        reason = "结果中 DOI 重复"
                    elif article_host != expected_host:
                        reason = "论文 URL 不是该期刊已验证官网"
                    elif abstract_evidence_url and abstract_evidence_host == "api.crossref.org" and (
                        abstract_evidence_type != "publisher_deposited_metadata"
                    ):
                        reason = "Crossref 摘要必须标记为 publisher_deposited_metadata"
                    elif abstract_evidence_url and abstract_evidence_host == "api.openalex.org" and (
                        abstract_evidence_type != "scholarly_metadata"
                    ):
                        reason = "OpenAlex 摘要必须标记为 scholarly_metadata"
                    elif abstract_evidence_type in {
                        "secondary_scholarly_metadata", "author_manuscript",
                    } and abstract_evidence_host not in SECONDARY_ABSTRACT_EVIDENCE_HOSTS:
                        reason = "补充摘要证据 URL 不在已核验的学术或作者来源中"
                    elif abstract_evidence_url and abstract_evidence_host not in {
                        expected_host, "api.crossref.org", "api.openalex.org",
                        *SECONDARY_ABSTRACT_EVIDENCE_HOSTS,
                    }:
                        reason = "摘要证据 URL 不是已验证官网或已核验元数据来源"
                    elif abstract_evidence_type and not abstract_evidence_url:
                        reason = "提供摘要证据类型时必须同时提供证据 URL"
                    elif decision["eligibility_status"] == "eligible" and (
                        len(abstract) < 80 or abstract.endswith(("...", "…"))
                    ):
                        if not abstract and (
                            abstract_unavailable or abstract_unavailable_traceable
                        ) and abstract_failure_reason:
                            pass
                        else:
                            reason = "研究论文缺少完整原始摘要；若已穷尽可用证据，必须记录可追溯缺失标记和原因"
                    else:
                        existing = db.execute("SELECT title FROM papers WHERE lower(doi)=?", (doi,)).fetchone()
                        if existing and not _title_matches(existing["title"], title):
                            reason = "DOI 已存在但标题不一致"
                    if reason:
                        errors.append({
                            "source": source_name, "issue": issue_key, "paper": paper_index,
                            "doi": doi, "reason": reason,
                        })
                        issue_failed = True
                        continue
                    seen_dois.add(doi)
                    authors = item.get("authors") if isinstance(item.get("authors"), list) else []
                    published = str(item.get("published") or "").strip()
                    if published:
                        try:
                            dt.date.fromisoformat(published)
                        except ValueError:
                            errors.append({
                                "source": source_name, "issue": issue_key, "paper": paper_index,
                                "doi": doi, "reason": "published 必须是 YYYY-MM-DD",
                            })
                            issue_failed = True
                            continue
                    accepted_papers.append({
                        "identity": "doi:" + doi,
                        "doi": doi,
                        "title": title,
                        "abstract": abstract,
                        "authors": [_clean(value) for value in authors if _clean(value)],
                        "published": published,
                        "url": article_url,
                        "abstract_evidence_url": abstract_evidence_url or article_url,
                        "abstract_evidence_type": abstract_evidence_type or "official_page",
                        "publication_type_raw": raw_type,
                        "abstract_unavailable_official": abstract_unavailable,
                        "abstract_unavailable_traceable": abstract_unavailable_traceable,
                        "abstract_failure_reason": abstract_failure_reason,
                        "decision": decision,
                    })
                if not issue_failed:
                    accepted.append({
                        "source_name": source_name,
                        "issue_key": issue_key,
                        "issue_url": issue_url,
                        "papers": accepted_papers,
                    })
        return {
            "source": str(path.expanduser().resolve()),
            "accepted": accepted,
            "skipped": skipped,
            "errors": errors,
            "counts": {
                "issues": len(accepted),
                "papers": sum(len(item["papers"]) for item in accepted),
                "skipped": len(skipped),
                "failed": len(errors),
            },
        }
    finally:
        db.close()


def apply_official_import(db_path: Path, preview: dict[str, Any]) -> dict[str, Any]:
    if preview.get("errors"):
        raise ValueError("官网导入预览仍有错误，未写入任何数据")
    db = connect(db_path)
    inserted = 0
    updated = 0
    try:
        now = utc_now()
        with db:
            for issue in preview["accepted"]:
                imported = 0
                for paper in issue["papers"]:
                    existing = db.execute(
                        "SELECT abstract FROM papers WHERE identity=?", (paper["identity"],)
                    ).fetchone()
                    prior_abstract = (existing["abstract"] or "") if existing else ""
                    decision = paper["decision"]
                    publisher_metadata = (
                        paper.get("abstract_evidence_type") == "publisher_deposited_metadata"
                    )
                    scholarly_metadata = paper.get("abstract_evidence_type") == "scholarly_metadata"
                    secondary_metadata = (
                        paper.get("abstract_evidence_type") == "secondary_scholarly_metadata"
                    )
                    author_manuscript = paper.get("abstract_evidence_type") == "author_manuscript"
                    abstract_source = (
                        "publisher-deposited-metadata" if publisher_metadata else (
                            "scholarly-metadata" if scholarly_metadata or secondary_metadata else (
                                "author-manuscript" if author_manuscript else "publisher-official"
                            )
                        )
                    ) if paper["abstract"] else "missing"
                    abstract_source_url = paper.get("abstract_evidence_url") if paper["abstract"] else None
                    db.execute(
                        """INSERT INTO papers(
                        identity,doi,title,abstract,venue,published,url,authors_json,first_seen,updated_at,
                        abstract_source,low_priority,low_priority_reason,publication_type,publication_type_raw,
                        publication_type_source,publication_type_evidence_json,eligibility_status,exclusion_reason,
                        abstract_source_url,abstract_retrieved_at,abstract_failure_reason,needs_rescreen
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(identity) DO UPDATE SET
                        doi=excluded.doi,title=excluded.title,venue=excluded.venue,
                        published=CASE WHEN excluded.published<>'' THEN excluded.published ELSE papers.published END,
                        url=excluded.url,authors_json=excluded.authors_json,updated_at=excluded.updated_at,
                        abstract=CASE WHEN length(excluded.abstract)>length(COALESCE(papers.abstract,'')) THEN excluded.abstract ELSE papers.abstract END,
                        abstract_source=CASE WHEN length(excluded.abstract)>length(COALESCE(papers.abstract,'')) THEN excluded.abstract_source ELSE papers.abstract_source END,
                        abstract_source_url=CASE WHEN length(excluded.abstract)>length(COALESCE(papers.abstract,'')) THEN excluded.abstract_source_url ELSE papers.abstract_source_url END,
                        abstract_retrieved_at=CASE WHEN length(excluded.abstract)>length(COALESCE(papers.abstract,'')) THEN excluded.abstract_retrieved_at ELSE papers.abstract_retrieved_at END,
                        abstract_failure_reason=CASE WHEN excluded.abstract<>'' THEN NULL ELSE papers.abstract_failure_reason END,
                        publication_type=excluded.publication_type,publication_type_raw=excluded.publication_type_raw,
                        publication_type_source=excluded.publication_type_source,
                        publication_type_evidence_json=excluded.publication_type_evidence_json,
                        eligibility_status=excluded.eligibility_status,exclusion_reason=excluded.exclusion_reason,
                        needs_rescreen=CASE WHEN length(excluded.abstract)>length(COALESCE(papers.abstract,'')) THEN 1 ELSE papers.needs_rescreen END""",
                        (
                            paper["identity"], paper["doi"], paper["title"], paper["abstract"],
                            issue["source_name"], paper["published"], paper["url"],
                            json.dumps(paper["authors"], ensure_ascii=False), now, now,
                            abstract_source, 0, None,
                            decision["publication_type"], paper["publication_type_raw"], "publisher-official",
                            json.dumps(decision["evidence"], ensure_ascii=False), decision["eligibility_status"],
                            decision.get("exclusion_reason"), abstract_source_url,
                            now if paper["abstract"] else None,
                            None if paper["abstract"] else (
                                paper.get("abstract_failure_reason") or "官网卷期未提供摘要"
                            ),
                            1 if decision["eligibility_status"] == "eligible" else 0,
                        ),
                    )
                    observation = issue["source_name"] + " / Official issue " + issue["issue_key"]
                    db.execute(
                        "INSERT OR IGNORE INTO observations(identity,source,observed_at) VALUES(?,?,?)",
                        (paper["identity"], observation, now),
                    )
                    if paper["abstract"]:
                        attempt_provider = (
                            "crossref" if publisher_metadata else (
                                "openalex" if scholarly_metadata else (
                                    "secondary-metadata" if secondary_metadata else (
                                        "author-manuscript" if author_manuscript else "official"
                                    )
                                )
                            )
                        )
                        db.execute(
                            """INSERT INTO abstract_attempts(
                            task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at
                            ) VALUES('official-issue-import',?,?, 'found',?,?,?,?)""",
                            (
                                paper["identity"], attempt_provider, abstract_source_url,
                                paper.get("abstract_evidence_type") or "official_page",
                                issue["issue_key"], now,
                            ),
                        )
                    elif paper.get("abstract_unavailable_official"):
                        db.execute(
                            """INSERT INTO abstract_attempts(
                            task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at
                            ) VALUES('official-issue-import',?,'official','not_found',?,'official_page',?,?)""",
                            (
                                paper["identity"], paper["url"],
                                paper.get("abstract_failure_reason") or "官网明确未提供摘要", now,
                            ),
                        )
                    elif paper.get("abstract_unavailable_traceable"):
                        db.execute(
                            """INSERT INTO abstract_attempts(
                            task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at
                            ) VALUES('official-issue-import',?,'official','not_found',?,'verified_metadata_gap',?,?)""",
                            (
                                paper["identity"], paper["url"],
                                paper.get("abstract_failure_reason") or "摘要暂不可得", now,
                            ),
                        )
                    if existing:
                        updated += int(len(paper["abstract"]) > len(prior_abstract))
                    else:
                        inserted += 1
                    imported += 1
                missing_official = sum(
                    paper["decision"]["eligibility_status"] == "eligible" and
                    not paper["abstract"] and paper.get("abstract_unavailable_official")
                    for paper in issue["papers"]
                )
                traceable_missing = sum(
                    paper["decision"]["eligibility_status"] == "eligible" and
                    not paper["abstract"] and paper.get("abstract_unavailable_traceable")
                    for paper in issue["papers"]
                )
                publisher_metadata_count = sum(
                    bool(paper["abstract"]) and
                    paper.get("abstract_evidence_type") == "publisher_deposited_metadata"
                    for paper in issue["papers"]
                )
                scholarly_metadata_count = sum(
                    bool(paper["abstract"]) and
                    paper.get("abstract_evidence_type") == "scholarly_metadata"
                    for paper in issue["papers"]
                )
                detail = "已逐篇核验官网标题与摘要"
                metadata_parts: list[str] = []
                if publisher_metadata_count:
                    metadata_parts.append(
                        f"{publisher_metadata_count} 篇摘要来自出版商提交的 Crossref 元数据"
                    )
                if scholarly_metadata_count:
                    metadata_parts.append(
                        f"{scholarly_metadata_count} 篇摘要由 DOI 精确匹配的 OpenAlex 学术元数据补充"
                    )
                if missing_official:
                    metadata_parts.append(f"{missing_official} 篇研究论文官网明确未提供摘要")
                if traceable_missing:
                    metadata_parts.append(f"{traceable_missing} 篇研究论文摘要暂不可得并已记录证据链")
                if metadata_parts:
                    detail = "已逐篇核验官网目录；" + "；".join(metadata_parts)
                db.execute(
                    """INSERT INTO official_issue_checks(
                    source_name,issue_key,issue_url,status,article_count,imported_count,detail,checked_at
                    ) VALUES(?,?,?,'succeeded',?,?,?,?)
                    ON CONFLICT(source_name,issue_key) DO UPDATE SET
                    issue_url=excluded.issue_url,status='succeeded',article_count=excluded.article_count,
                    imported_count=excluded.imported_count,detail=excluded.detail,checked_at=excluded.checked_at""",
                    (issue["source_name"], issue["issue_key"], issue["issue_url"], len(issue["papers"]),
                     imported, detail, now),
                )
        return {
            "issues": len(preview["accepted"]),
            "papers": sum(len(item["papers"]) for item in preview["accepted"]),
            "inserted": inserted,
            "abstracts_updated": updated,
            "skipped": len(preview.get("skipped", [])),
        }
    finally:
        db.close()
