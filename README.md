# Personal Academic Radar

A local-first academic monitoring system for one researcher. It collects papers
from Crossref, OpenAlex, and CHI proceedings, keeps durable SQLite history, and
uses the model already available inside a Codex Automation for semantic
relevance judgments. No separate LLM API key is required for the recommended
workflow.

This repository contains application code only. Your configuration, research
profile, feedback, database, logs, and generated digests belong in an external
state directory (by default `~/.local/share/personal-academic-radar`) and are
excluded from version control.

## Current status

The existing collector and Codex export/import workflow remain available while
the project is being evolved into the complete web application. Database
migrations, verified backups, and legacy-state migration are already exposed by
the `academic-radar` command.

Collection is cursor-paginated and records per-source health. Crossref and
OpenAlex are attempted independently, so a Crossref outage can degrade a source
without discarding usable OpenAlex results.

## Quick start

Requires Python 3.9 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
mkdir -p ~/.local/share/personal-academic-radar
cp assets/config.example.toml ~/.local/share/personal-academic-radar/config.toml
cp assets/research-profile.example.md ~/.local/share/personal-academic-radar/research-profile.md
academic-radar db upgrade --db ~/.local/share/personal-academic-radar/papers.sqlite3
python scripts/paper_monitor.py doctor --config ~/.local/share/personal-academic-radar/config.toml
```

To migrate an older project-local `state/` directory without modifying it:

```bash
academic-radar state migrate \
  --from ./state \
  --to ~/.local/share/personal-academic-radar
```

The command makes a consistent SQLite backup, verifies it, copies non-secret
state artifacts, and then applies versioned schema migrations to the new copy.
It refuses to overwrite an existing destination unless `--merge` is supplied.

## Recommended daily workflow

The scheduled Codex task should:

1. Run `agent-export` to collect and create a semantic screening queue.
2. Read the complete active research profile and confirmed feedback examples.
3. Judge every queued paper with the host model.
4. Run `agent-import` to validate and persist the judgments.

Do not use the legacy `run` command without an API-backed model: its heuristic
fallback exists only for backward compatibility and is not the product's
recommended screening path.

## Data safety

- Never commit the state directory or `.env` files.
- Credentials are read only from environment variables or the system keychain.
- DOI is the preferred paper identity; normalized title hashes are the fallback.
- Backups are created with SQLite's online backup API and checked with
  `PRAGMA integrity_check` before success is reported.
- Restore requires an explicit `--replace` flag and preserves the current
  database as a timestamped pre-restore backup.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m academic_radar.cli db status --db /path/to/papers.sqlite3
```

See [docs/architecture.md](docs/architecture.md) for the system boundaries and
planned product modules.
