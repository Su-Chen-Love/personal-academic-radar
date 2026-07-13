# Deployment and daily operation

## Supported daily architecture

The recommended single-researcher installation has three explicit boundaries:

1. this public code repository;
2. a private state directory containing configuration, SQLite, queues, results,
   feedback, logs, digests, and backups; and
3. the Codex Automation that runs export, host-model semantic judgment, and
   atomic import every morning.

No `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is required. The old heuristic
fallback is not part of the supported daily workflow and the direct runner now
fails closed when no semantic API provider is explicitly configured.

## Local installation

Create a virtual environment, install the package, copy the example config and
profile to `~/.local/share/personal-academic-radar`, then run:

```bash
academic-radar db upgrade --db ~/.local/share/personal-academic-radar/papers.sqlite3
python scripts/paper_monitor.py doctor --config ~/.local/share/personal-academic-radar/config.toml
academic-radar verify --config ~/.local/share/personal-academic-radar/config.toml
academic-radar web --config ~/.local/share/personal-academic-radar/config.toml
```

The web app listens on `127.0.0.1:8765` by default. Do not use
`--allow-remote` without adding an authenticated reverse proxy and reviewing
the threat model.

## Codex Automation

Schedule one local project automation daily at 08:00. Its prompt must:

- read `SKILL.md` and `references/profile-guidance.md`;
- call `agent-export` with the private config;
- read the complete confirmed profile and `feedback_examples`;
- semantically judge every queued identity with the host model;
- preserve run, profile, and source-failure metadata;
- write results only into the private state directory; and
- call `agent-import`, then report collection, candidate, selected, and failure
  counts.

A newer export abandons an unfinished older queue. Import is all-or-nothing and
rejects partial, duplicate, stale, or mismatched results.

## Backups and recovery

Create a consistent, verified backup before migrations and periodically during
normal use:

```bash
academic-radar db backup \
  --db ~/.local/share/personal-academic-radar/papers.sqlite3 \
  --output ~/.local/share/personal-academic-radar/backups/manual.sqlite3
```

Test a backup without touching production by restoring to a temporary path and
running `db status`. Restoring over production requires `--replace`; the tool
first preserves the current database as a timestamped pre-restore backup.

Never synchronize a live SQLite WAL database with ordinary file-copy tools.
Use the built-in online backup operation.

## macOS web service

`assets/com.personal-academic-radar.web.plist` is a launchd template. Replace
`__PROJECT_ROOT__` and `__STATE_DIR__` with absolute paths, place the rendered
file in `~/Library/LaunchAgents`, and load it with the normal macOS service
management command. It starts only the local web interface; Codex remains the
semantic scheduler.

## Public or remote access

Public access is intentionally not enabled by default because the application
contains a private research profile and behavioral feedback. Choose and approve
an authentication and persistence design before deployment. A public code
repository must never include the state directory, configuration, database,
queues, results, logs, or backups.

## Source behavior

Crossref uses cursor pagination and an overlapping created-date window.
OpenAlex is attempted independently, so one provider may degrade while the
other still supplies records. Existing abstracts are reused before DOI-level
enrichment. Inspect the Sources and Run Status pages or the `source_health`,
`source_runs`, and `pipeline_runs` tables when a source is degraded.
