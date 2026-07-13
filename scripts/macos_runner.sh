#!/bin/zsh
set -eu

STATE_DIR="${PAPER_MONITOR_STATE_DIR:-$HOME/.local/share/research-paper-monitor}"
PYTHON_BIN="${PAPER_MONITOR_PYTHON:-/usr/bin/python3}"

keychain_value() {
  /usr/bin/security find-generic-password -s "$1" -w 2>/dev/null || true
}

OPENAI_API_KEY="${OPENAI_API_KEY:-$(keychain_value research-paper-monitor-openai)}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(keychain_value research-paper-monitor-anthropic)}"
PAPER_MONITOR_SMTP_PASSWORD="${PAPER_MONITOR_SMTP_PASSWORD:-$(keychain_value research-paper-monitor-gmail)}"
PAPER_MONITOR_SMTP_USERNAME="${PAPER_MONITOR_SMTP_USERNAME:-}"
export OPENAI_API_KEY ANTHROPIC_API_KEY PAPER_MONITOR_SMTP_PASSWORD PAPER_MONITOR_SMTP_USERNAME

exec "$PYTHON_BIN" "$STATE_DIR/run/paper_monitor.py" run --config "$STATE_DIR/config.toml" "$@"
