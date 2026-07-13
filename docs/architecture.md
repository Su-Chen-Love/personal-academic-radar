# Architecture

Personal Academic Radar is deliberately local-first and single-user.

## Trust and data boundaries

- **Public repository:** source code, migrations, tests, generic examples, and
  deployment documentation.
- **Private state directory:** SQLite database, configuration, profile text,
  feedback, queues, model results, logs, and digests.
- **Codex Automation:** reads a queue plus the confirmed profile and feedback
  examples, performs semantic judgment with the host model, then imports strict
  JSON. It does not need an external model API key.
- **External metadata providers:** Crossref is primary; OpenAlex enriches and
  fills gaps; CHI is collected through proceedings metadata with explicit
  validation.

## Planned application modules

1. Collection adapters with retries, pagination, deduplication, and health.
2. Versioned storage and recoverable pipeline runs.
3. Semantic queue export/import and confirmed profile versions.
4. Feedback and reading-state services.
5. A local single-user web application and JSON API.
6. Deployment, backup, and scheduled-operation tooling.

The SQLite schema is portable and all user-facing state can be exported. Model
labels and screening evidence are stored as data, not embedded in business
logic, so changing the host model does not require migrating paper history.

