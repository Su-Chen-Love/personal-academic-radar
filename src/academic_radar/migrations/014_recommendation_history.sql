-- Selection membership and its judgment survive later re-screening.
CREATE TABLE recommendation_snapshots(
  run_id TEXT NOT NULL REFERENCES agent_jobs(run_id),
  identity TEXT NOT NULL REFERENCES papers(identity),
  score REAL,
  reasons TEXT,
  confidence REAL,
  themes_json TEXT NOT NULL DEFAULT '[]',
  screened_at TEXT,
  reasoning_json TEXT NOT NULL DEFAULT '{}',
  score_dimensions_json TEXT NOT NULL DEFAULT '{}',
  rubric_version TEXT,
  evidence_source TEXT NOT NULL DEFAULT 'unavailable',
  PRIMARY KEY(run_id, identity)
);
CREATE INDEX agent_jobs_history ON agent_jobs(status, imported_at);

INSERT INTO recommendation_snapshots(
  run_id,identity,score,reasons,confidence,themes_json,screened_at,
  reasoning_json,score_dimensions_json,rubric_version,evidence_source
)
SELECT rp.run_id,rp.identity,s.score,s.reasons,s.confidence,COALESCE(s.themes_json,'[]'),
  s.screened_at,COALESCE(s.reasoning_json,'{}'),COALESCE(s.score_dimensions_json,'{}'),
  s.rubric_version,CASE WHEN s.identity IS NULL THEN 'unavailable' ELSE 'screening' END
FROM (SELECT DISTINCT run_id,identity FROM run_papers WHERE role IN ('selected','selected_new')) rp
JOIN agent_jobs aj ON aj.run_id=rp.run_id AND aj.status='imported'
LEFT JOIN screenings s ON s.rowid=(
  SELECT old.rowid FROM screenings old
  WHERE old.run_id=rp.run_id AND old.identity=rp.identity
    AND old.profile_hash=aj.profile_hash AND old.provider='codex-agent'
  ORDER BY old.screened_at DESC,old.rowid DESC LIMIT 1
);
