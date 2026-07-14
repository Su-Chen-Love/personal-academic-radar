ALTER TABLE papers ADD COLUMN publication_type TEXT NOT NULL DEFAULT 'Unknown';
ALTER TABLE papers ADD COLUMN publication_type_raw TEXT;
ALTER TABLE papers ADD COLUMN publication_type_source TEXT;
ALTER TABLE papers ADD COLUMN publication_type_evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE papers ADD COLUMN eligibility_status TEXT NOT NULL DEFAULT 'quarantine'
  CHECK(eligibility_status IN ('eligible','excluded','quarantine'));
ALTER TABLE papers ADD COLUMN exclusion_reason TEXT;
ALTER TABLE papers ADD COLUMN abstract_source_url TEXT;
ALTER TABLE papers ADD COLUMN abstract_retrieved_at TEXT;
ALTER TABLE papers ADD COLUMN abstract_failure_reason TEXT;
ALTER TABLE papers ADD COLUMN needs_rescreen INTEGER NOT NULL DEFAULT 0
  CHECK(needs_rescreen IN (0,1));

CREATE TABLE IF NOT EXISTS abstract_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  identity TEXT NOT NULL,
  provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('found','not_found','failed','skipped')),
  source_url TEXT,
  evidence_type TEXT,
  detail TEXT,
  attempted_at TEXT NOT NULL,
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_abstract_attempt_identity
  ON abstract_attempts(identity, attempted_at DESC);

CREATE TABLE IF NOT EXISTS task_runs(
  task_id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','partial','failed')),
  total_count INTEGER NOT NULL DEFAULT 0,
  completed_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_created ON task_runs(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_task_type
  ON task_runs(task_type) WHERE status='running';

CREATE TABLE IF NOT EXISTS cleanup_audits(
  audit_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('preview','applied','restored')),
  backup_path TEXT NOT NULL,
  report_path TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT,
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE TABLE IF NOT EXISTS run_papers(
  run_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('collected','new','candidate','selected','selected_new')),
  PRIMARY KEY(run_id,identity,role),
  FOREIGN KEY(identity) REFERENCES papers(identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_papers_role ON run_papers(run_id,role);
