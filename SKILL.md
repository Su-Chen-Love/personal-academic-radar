---
name: monitor-research-papers
description: Monitor configured academic journals and conferences for newly published papers, maintain a local SQLite history, infer or update a research-interest profile from user papers and project materials, screen titles and abstracts with an LLM or deterministic fallback, produce daily Markdown/HTML digests, and optionally email relevant results. Use when Codex or Claude needs to set up, run, troubleshoot, schedule, or review an automated literature-monitoring workflow.
---

# Monitor Research Papers

Use the bundled deterministic runner for collection, normalization, deduplication, persistence, retry, and digest creation. In Claude/Codex scheduled tasks, use the host model itself for relevance judgment through the agent export/import workflow; this requires no separate LLM API key.

## Workflow

1. Locate the skill directory and choose a writable state directory outside it. Default to `~/.local/share/research-paper-monitor`.
2. If no configuration exists, copy `assets/config.example.toml` to `<state>/config.toml` and `assets/research-profile.example.md` to `<state>/research-profile.md`. Never overwrite an existing profile or database.
3. Refine the profile from user-provided papers, proposals, notes, and explicit feedback. Read `references/profile-guidance.md` before changing it. Preserve negative interests and boundary conditions, not just keywords.
4. Validate without changing state:
   `python3 scripts/paper_monitor.py doctor --config <state>/config.toml`
5. Preview collection when changing sources:
   `python3 scripts/paper_monitor.py run --config <state>/config.toml --dry-run`
6. For a Claude/Codex scheduled task, export papers awaiting model judgment:
   `python3 scripts/paper_monitor.py agent-export --config <state>/config.toml`
   Read the returned queue JSON and complete every paper using the rubric in `references/profile-guidance.md`. Write a results JSON with the same `run_id` and `profile_hash`, a `model` label, and a `results` array. Each result must contain `identity`, `relevant`, `score`, `reasons`, `matched_themes`, and `confidence`.
7. Import the judgments and generate the digest:
   `python3 scripts/paper_monitor.py agent-import --config <state>/config.toml --results <results.json>`
   After materially changing the profile, add `--rescreen` to the export once.
8. Report the collection count, candidate count, relevant papers with title/abstract/reason, digest path, and source failures. Do not claim success if required sources failed.

## Safety and operational rules

- Keep API keys and SMTP credentials in environment variables. Never write secrets into the skill, config, database, logs, or digest.
- Default the first successful run to baseline mode: persist observed papers without emailing old backlog. Set `bootstrap_mode = "include"` only when the user explicitly wants historical results.
- Treat DOI as the preferred identity; otherwise use a normalized title hash. Upserts must be idempotent.
- Prefer Crossref for DOI/publisher metadata and use OpenAlex as a fallback/enrichment source. Do not scrape publisher HTML unless the user explicitly accepts brittle scraping and applicable terms permit it.
- A source failure must not erase prior state or mark unseen papers as processed. Retry transient errors and continue other sources.
- Prefer the host-model agent export/import workflow in Claude/Codex automations. Use direct API providers only for headless operation outside an AI host. Never silently replace requested semantic model judgment with keyword matching.
- Send email only when `delivery.enabled = true`; respect `send_when_empty`.
- SQLite is single-host state. For GitHub Actions, persist the state directory with an artifact/cache or use an external durable store; otherwise every run bootstraps anew.

## Research-interest judgment

Read `references/profile-guidance.md` whenever building or materially revising a profile. Pass the complete profile, title, abstract, venue, and publication type to the model. Require strict JSON containing `relevant`, `score`, `reasons`, `matched_themes`, and `confidence`. The runner validates and bounds these fields.

Favor papers with a meaningful connection to the research questions, methods, or empirical paradigms. A venue match alone is not relevance. Broad HCI work without a link to decision support, preference elicitation/integration, interactive optimization, human-AI collaboration, natural-language grounding, mixed initiative, trust/calibration, or vehicle-routing operations should normally rank lower.

## Scheduling

The skill itself performs one idempotent cycle and does not impersonate a scheduler. For Codex or Claude scheduled-task features, schedule the agent export/judge/import workflow daily. For a headless workstation, use the direct-API instructions in `references/deployment.md`. For GitHub Actions, adapt `assets/github-actions-daily.yml` and configure encrypted repository secrets.

## Troubleshooting

Read `references/deployment.md` for provider, email, scheduling, and persistence details. Run `doctor` before diagnosis. Inspect the latest JSON run log in `<state>/logs/` and the `source_runs` table; avoid deleting the database as a first response.
