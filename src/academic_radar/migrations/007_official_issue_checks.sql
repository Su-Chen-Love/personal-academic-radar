CREATE TABLE IF NOT EXISTS official_issue_checks(
  source_name TEXT NOT NULL,
  issue_key TEXT NOT NULL,
  issue_url TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('succeeded','partial','failed')),
  article_count INTEGER NOT NULL DEFAULT 0,
  imported_count INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  checked_at TEXT NOT NULL,
  PRIMARY KEY(source_name,issue_key)
);
CREATE INDEX IF NOT EXISTS idx_official_issue_checks_time
  ON official_issue_checks(source_name,checked_at DESC);
