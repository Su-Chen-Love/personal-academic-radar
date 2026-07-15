CREATE TABLE IF NOT EXISTS profile_review_runs(
  fingerprint TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('no_change','suggested','accepted','dismissed')),
  feedback_count INTEGER NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  profile_version_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(profile_version_id) REFERENCES profile_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_profile_review_status
  ON profile_review_runs(status,updated_at DESC);
