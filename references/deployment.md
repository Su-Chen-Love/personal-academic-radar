# Deployment and operations

## Local setup

Requires Python 3.9+ and uses only the standard library. Copy the example config and profile into a writable state directory. Run `doctor`, then `run --dry-run`, then a normal run. The SQLite database, digests, and logs live under `state_dir`.

Install the complete `monitor-research-papers` folder in `~/.claude/skills/` for Claude, or `${CODEX_HOME:-~/.codex}/skills/` for Codex. A project-local installation may instead live under `.claude/skills/` or the skill directory configured by the host. Keep the folder name unchanged.

In Codex or Claude scheduled tasks, prefer `agent-export` followed by host-model judgment and `agent-import`. This uses the model already supplied by the scheduled task and does not require `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

Direct API selection is only needed for headless runs outside an AI host:

- `provider = "auto"`: use OpenAI when `OPENAI_API_KEY` exists, otherwise Anthropic when `ANTHROPIC_API_KEY` exists, otherwise deterministic scoring.
- `provider = "openai"`: call the OpenAI Responses API using `api_key_env`.
- `provider = "anthropic"`: call the Anthropic Messages API using `anthropic_api_key_env`.
- `provider = "heuristic"`: never make an LLM call.

For Gmail SMTP, create an app password after enabling two-step verification. Set `PAPER_MONITOR_SMTP_USERNAME` and `PAPER_MONITOR_SMTP_PASSWORD`; never use the normal Google password.

On macOS, use `scripts/macos_runner.sh` from `launchd`. It reads optional secrets from Keychain without writing them into the plist. Add only the credentials you intend to use:

```bash
security add-generic-password -U -a "$USER" -s research-paper-monitor-openai -w
security add-generic-password -U -a "$USER" -s research-paper-monitor-anthropic -w
security add-generic-password -U -a "$USER" -s research-paper-monitor-gmail -w
```

Each command securely prompts for the value. OpenAI and Anthropic are alternatives; one LLM key is enough. The Gmail entry must be a Google app password, not the account password.

After changing the research profile, model, or relevance threshold, run once with `--rescreen` to evaluate the stored corpus again. This does not duplicate paper records; previously emailed papers remain marked as sent.

## Scheduler examples

Cron, daily at 08:00:

```cron
0 8 * * * /usr/bin/python3 /absolute/path/monitor-research-papers/scripts/paper_monitor.py run --config /absolute/path/state/config.toml >> /absolute/path/state/cron.log 2>&1
```

On macOS prefer `launchd`; on Linux prefer a user `systemd` timer. In Claude or Codex, create a daily scheduled task whose prompt invokes this skill and runs the same command. A sleeping laptop cannot execute local schedules.

## GitHub Actions

Copy `assets/github-actions-daily.yml` to `.github/workflows/paper-monitor.yml`. Add repository secrets for LLM and SMTP credentials. The template restores and saves the state directory via Actions cache, but cache storage is not a transactional database backup. For critical history, upload an encrypted artifact or use durable external storage.

## Collection behavior

Crossref filters use online publication dates and a lookback overlap; SQLite dedup makes overlapping windows safe. Crossref may omit abstracts. With `openalex_fallback = true`, the runner queries OpenAlex by DOI to reconstruct an abstract when available. Conference proceedings metadata is less uniform than journal metadata, so periodically inspect CHI results and adjust `query_container` if false positives appear.

The phrase “International Journal of Industrial Economics” is ambiguous and does not match a well-known title in the requested area. The example uses *International Journal of Industrial Ergonomics*. Replace it if a different journal was intended.
