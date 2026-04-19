#!/usr/bin/env bash
# Claude Code Notification hook -> Home Assistant webhook.
#
# Wired via ~/.claude/settings.json:
#   "Notification": [{ "matcher": "permission_prompt|idle_prompt",
#     "hooks": [{ "type": "command",
#       "command": "~/Desktop/CODING/Privat/claude-ha-bridge/hooks/notify.sh" }]}]
#
# Reads Claude's hook JSON from stdin. Registers the current tmux pane so the
# daemon can route button-actions back to the correct Claude session.

set -euo pipefail

CONFIG_DIR="${HOME}/.config/claude-ha-bridge"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SESSIONS_DIR="${CONFIG_DIR}/sessions"

[[ -f "$CONFIG_FILE" ]] || {
  echo "claude-ha-bridge: config missing, skipping notify" >&2
  exit 0
}

HA_URL=$(jq -r '.ha_url' "$CONFIG_FILE")
WEBHOOK_ID=$(jq -r '.webhook_id' "$CONFIG_FILE")
[[ -n "$HA_URL" && -n "$WEBHOOK_ID" ]] || {
  echo "claude-ha-bridge: ha_url or webhook_id missing in config" >&2
  exit 0
}

# Claude's hook payload (stdin) -- best effort; fall back if not piped
PAYLOAD=$(cat 2>/dev/null || true)
SESSION_ID=$(echo "$PAYLOAD" | jq -r '.session_id // empty' 2>/dev/null || true)
EVENT=$(echo "$PAYLOAD" | jq -r '.hook_event_name // "notification"' 2>/dev/null || echo "notification")
MESSAGE=$(echo "$PAYLOAD" | jq -r '.message // .notification.message // empty' 2>/dev/null || true)

# Tag must be non-empty and stable for routing -- use session_id if available,
# otherwise fall back to the project path hash.
PROJECT=$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")
TAG="${SESSION_ID:-$(echo "$PROJECT" | shasum | cut -c1-12)}"

# Register tmux target if we're in tmux -- daemon reads this when a button
# action arrives.
if [[ -n "${TMUX:-}" ]]; then
  TMUX_SESSION=$(tmux display-message -p '#S' 2>/dev/null || true)
  TMUX_PANE=$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)
  if [[ -n "$TMUX_SESSION" && -n "$TMUX_PANE" ]]; then
    mkdir -p "$SESSIONS_DIR"
    jq -n \
      --arg target "$TMUX_PANE" \
      --arg session "$TMUX_SESSION" \
      --arg project "$PROJECT" \
      --arg cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
      '{tmux_target: $target, tmux_session: $session, project: $project, cwd: $cwd}' \
      > "${SESSIONS_DIR}/${TAG}.json"
  fi
fi

TITLE="Claude: ${PROJECT}"
BODY="${MESSAGE:-${EVENT}}"

# Fire webhook; HA blueprint picks it up and pushes the actionable notification.
curl -fsS -m 5 -X POST "${HA_URL%/}/api/webhook/${WEBHOOK_ID}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
        --arg title "$TITLE" \
        --arg message "$BODY" \
        --arg tag "$TAG" \
        --arg event "$EVENT" \
        '{title: $title, message: $message, tag: $tag, event: $event}')" \
  >/dev/null 2>&1 || true
