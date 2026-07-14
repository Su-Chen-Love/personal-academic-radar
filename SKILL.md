---
name: monitor-research-papers
description: Monitor configured academic journals and conferences for newly published papers, maintain private local SQLite history, enrich traceable abstracts, govern publication types, and use the Codex host export/judge/import workflow for relevance. Use when Codex needs to set up, run, troubleshoot, schedule, or review this local literature-monitoring workflow.
---

# Monitor Research Papers

Use the bundled deterministic runner for collection, normalization, deduplication, persistence, retry, and digest creation. In Claude/Codex scheduled tasks, use the host model itself for relevance judgment through the agent export/import workflow; this requires no separate LLM API key.

## Workflow

1. Locate the skill directory and choose a writable state directory outside it. Default to `~/.local/share/research-paper-monitor`.
2. If no configuration exists, copy `assets/config.example.toml` to `<state>/config.toml` and `assets/research-profile.example.md` to `<state>/research-profile.md`. Never overwrite an existing profile or database.
3. Refine the profile from user-provided papers, proposals, notes, and explicit feedback. Read `references/profile-guidance.md` before changing it. Preserve negative interests and boundary conditions, not just keywords.
4. Validate without changing state:
   `python3 scripts/paper_monitor.py doctor --config <state>/config.toml`
5. Enrich traceable abstracts and publication-type evidence:
   `academic-radar abstracts enrich --config <state>/config.toml`
6. For a Codex scheduled task, export papers awaiting model judgment:
   `python3 scripts/paper_monitor.py agent-export --config <state>/config.toml`
   Read the returned queue JSON and complete every paper using the rubric in `references/profile-guidance.md`. Write a results JSON with the same `run_id` and `profile_hash`, a `model` label, and a `results` array. Each result must contain `identity`, `relevant`, `score`, `reasons`, `matched_themes`, and `confidence`.
   Treat `feedback_examples` as confirmed positive/negative calibration evidence,
   while keeping the confirmed profile as the primary decision rubric. Never
   change the profile from feedback without creating a draft for user approval.
7. Import the judgments and generate the digest:
   `python3 scripts/paper_monitor.py agent-import --config <state>/config.toml --results <results.json>`
   After materially changing the profile, add `--rescreen` to the export once.
8. Report the collection count, candidate count, relevant papers with title/abstract/reason, digest path, and source failures. Do not claim success if required sources failed.

## Safety and operational rules

- Never add a direct model provider or inspect/output environment-variable secrets. Semantic judgment is performed only by the Codex host export/import workflow.
- Default the first successful run to baseline mode: persist observed papers without emailing old backlog. Set `bootstrap_mode = "include"` only when the user explicitly wants historical results.
- Treat DOI as the preferred identity; otherwise use a normalized title hash. Upserts must be idempotent.
- Prefer Crossref for DOI/publisher metadata, then OpenAlex, Semantic Scholar, Europe PMC, PubMed, and verified publisher structured metadata. Store only clearly identified original abstracts with source URL and time; never treat search snippets or generated summaries as abstracts.
- A source failure must not erase prior state or mark unseen papers as processed. Retry transient errors and continue other sources.
- Use only the host-model agent export/import workflow in Codex automations. Never silently replace semantic judgment with keyword matching.
- Imports are all-or-nothing. They must cover every exported identity exactly
  once and preserve run, profile, feedback snapshot, and source-failure metadata.
  A newer export abandons any older unfinished queue.
- Send email only when `delivery.enabled = true`; respect `send_when_empty`.
- SQLite is private single-host state. Never upload it, its WAL, queues, results, logs, PDFs, feedback, or configuration to GitHub or a synchronization service.

## Research-interest judgment

Read `references/profile-guidance.md` whenever building or materially revising a profile. Pass the complete profile, title, abstract, venue, and publication type to the model. Require strict JSON containing `relevant`, `score`, `reasons`, `matched_themes`, and `confidence`. The runner validates and bounds these fields.

Favor papers with a meaningful connection to the research questions, methods, or empirical paradigms. A venue match alone is not relevance. Broad HCI work without a link to decision support, preference elicitation/integration, interactive optimization, human-AI collaboration, natural-language grounding, mixed initiative, trust/calibration, or vehicle-routing operations should normally rank lower.

## Scheduling

The skill itself performs one idempotent cycle and does not impersonate a
scheduler. Use a Codex scheduled task for the daily export/judge/import workflow
and the local web service for browsing and feedback. The supported personal
workflow does not require a model API key; see `references/deployment.md` for
backup, recovery, and service guidance.

## Troubleshooting

Read `references/deployment.md` for metadata sources, scheduling, recovery, and persistence details. Run `doctor` or `academic-radar verify` before diagnosis. Inspect the latest JSON run log in `<state>/logs/` and the `source_runs`, `task_runs`, and `abstract_attempts` tables; avoid deleting the database as a first response.
