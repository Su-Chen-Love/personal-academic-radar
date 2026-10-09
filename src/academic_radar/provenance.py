"""Verify missing abstract provenance without replacing or inventing an abstract."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .enrichment import MetadataClient, ProviderTemporarilyUnavailable, EnrichmentBudgetExceeded, lookup_crossref, lookup_openalex, clean_abstract
from .storage import connect, utc_now


def verify_provenance(db_path: Path, config: dict, limit: int = 100, budget: int = 180) -> dict[str, Any]:
    db = connect(db_path)
    candidates = [dict(r) for r in db.execute("""SELECT identity,doi,title,abstract,url FROM papers
      WHERE COALESCE(abstract,'')<>'' AND (COALESCE(abstract_source_url,'')='' OR COALESCE(abstract_retrieved_at,'')='')
      AND NOT EXISTS(SELECT 1 FROM abstract_attempts a WHERE a.identity=papers.identity AND a.provider='provenance-review'
        AND julianday(a.attempted_at)>julianday('now',CASE WHEN a.status='failed' THEN '-1 day' ELSE '-7 days' END))
      ORDER BY eligibility_status='eligible' DESC,published DESC,identity LIMIT ?""", (limit,))]
    client = MetadataClient(config)
    client.timeout = min(client.timeout, 10)
    client.retries = 0
    client.deadline = time.monotonic() + budget
    verified = checked = failed = 0
    task_id = "provenance-" + utc_now()
    try:
        for paper in candidates:
            if time.monotonic() >= client.deadline:
                break
            found = None
            network_failed = False
            unavailable = 0
            for lookup in (lookup_crossref, lookup_openalex):
                try:
                    result = lookup(db, paper, client)
                except (ProviderTemporarilyUnavailable, EnrichmentBudgetExceeded):
                    unavailable += 1
                    network_failed = True
                    result = None
                except (TimeoutError, OSError, RuntimeError, ValueError):
                    network_failed = True
                    result = None
                if result and result.get("abstract") and clean_abstract(result["abstract"]) == clean_abstract(paper["abstract"]):
                    found = result
                    break
            if unavailable == 2:
                # Do not mark every remaining paper as attempted while both
                # providers are cooling down or the task budget is exhausted.
                break
            stamp = utc_now()
            with db:
                # Recheck content to avoid overwriting concurrent manual/publisher evidence.
                if found:
                    change = db.execute("""UPDATE papers SET abstract_source=?,abstract_source_url=?,abstract_retrieved_at=?
                      WHERE identity=? AND abstract=? AND (COALESCE(abstract_source_url,'')='' OR COALESCE(abstract_retrieved_at,'')='')""",
                      (found["source_name"], found["source_url"], stamp, paper["identity"], paper["abstract"]))
                    verified += change.rowcount
                db.execute("""INSERT INTO abstract_attempts(task_id,identity,provider,status,source_url,evidence_type,detail,attempted_at)
                  VALUES(?,?,'provenance-review',?,?,?,?,?)""",
                  (task_id, paper["identity"], "found" if found else "failed" if network_failed else "not_found", found["source_url"] if found else None,
                   found["evidence_type"] if found else None,
                   "Original abstract exactly matched the retrieved DOI record" if found else
                   "Provider unavailable; original record preserved for retry" if network_failed else
                   "No exact original-abstract match from Crossref/OpenAlex; original record preserved", stamp))
            checked += 1
            failed += int(network_failed and not found)
        remaining = db.execute("""SELECT COUNT(*) FROM papers WHERE COALESCE(abstract,'')<>'' AND
          (COALESCE(abstract_source_url,'')='' OR COALESCE(abstract_retrieved_at,'')='')""").fetchone()[0]
        return {"status": "succeeded" if checked == len(candidates) and not failed else "partial", "checked": checked,
                "verified": verified, "deferred": len(candidates)-checked, "remaining_provenance_gaps": remaining}
    finally:
        db.close()
