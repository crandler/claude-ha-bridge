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

# Keep every file we create restricted to the current user -- session
# files and the debug log contain paths, session ids and prompt text.
umask 077

CONFIG_DIR="${HOME}/.config/claude-ha-bridge"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SESSIONS_DIR="${CONFIG_DIR}/sessions"
NOTIFY_LOG="${CONFIG_DIR}/notify.log"

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
NOTIF_TYPE=$(echo "$PAYLOAD" | jq -r '.notification_type // empty' 2>/dev/null || true)
TRANSCRIPT_PATH=$(echo "$PAYLOAD" | jq -r '.transcript_path // empty' 2>/dev/null || true)

# Dump payload for debugging -- lets us evolve title/body once we see what
# Claude actually sends for permission/idle prompts.
mkdir -p "$CONFIG_DIR"
{
  printf '%s --- %s\n' "$(date -u +%FT%TZ)" "$EVENT"
  echo "$PAYLOAD"
  echo
} >> "$NOTIFY_LOG" 2>/dev/null || true
# Belt-and-braces: enforce 600 even if the file was created pre-umask.
chmod 600 "$NOTIFY_LOG" 2>/dev/null || true

# Tag is stable per Claude session and used for iOS push grouping only --
# NOT for authorising button presses. Falls back to a project path hash
# when Claude did not provide a session_id.
PROJECT=$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")
# Hash the full project dir so two projects with the same basename do not
# collide. SHA-1 is fine here: we only need collision-resistance for
# routing, not crypto strength.
TAG="${SESSION_ID:-$(printf '%s' "${CLAUDE_PROJECT_DIR:-$PWD}" | shasum -a 256 | cut -c1-12)}"

# Generate a fresh one-shot token for this notification. The token is the
# only thing the daemon trusts to authorise a button press: it is stored
# in the session file below and must match bit-for-bit on the resulting
# mobile_app_notification_action event. Any replay or forged event with
# an unknown token is silently dropped.
TOKEN=$(openssl rand -hex 16)

# Register tmux target if we're in tmux -- daemon reads this when a button
# action arrives. Write via a temp file + rename so the cleanup loop
# never sees a half-populated session file mid-write.
if [[ -n "${TMUX:-}" ]]; then
  TMUX_SESSION=$(tmux display-message -p '#S' 2>/dev/null || true)
  TMUX_PANE=$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)
  if [[ -n "$TMUX_SESSION" && -n "$TMUX_PANE" ]]; then
    mkdir -p "$SESSIONS_DIR"
    TMP_SESSION=$(mktemp "${SESSIONS_DIR}/.${TAG}.XXXXXX") || TMP_SESSION=""
    if [[ -n "$TMP_SESSION" ]]; then
      chmod 600 "$TMP_SESSION" 2>/dev/null || true
      jq -n \
        --arg target "$TMUX_PANE" \
        --arg session "$TMUX_SESSION" \
        --arg project "$PROJECT" \
        --arg cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
        --arg token "$TOKEN" \
        --arg session_id "$SESSION_ID" \
        '{tmux_target: $target, tmux_session: $session, project: $project,
          cwd: $cwd, token: $token, session_id: $session_id}' \
        > "$TMP_SESSION"
      mv -f "$TMP_SESSION" "${SESSIONS_DIR}/${TAG}.json"
    fi
  fi
fi

# Short session identifier so parallel Claude sessions are distinguishable
# on the lock screen ("Claude - project - ab12cd").
SHORT_ID="${SESSION_ID:0:6}"
[[ -n "$SHORT_ID" ]] || SHORT_ID=$(echo "$PROJECT" | shasum -a 256 | cut -c1-6)

TITLE="Claude - ${PROJECT} - ${SHORT_ID}"
BODY="${MESSAGE:-${EVENT}}"

# For permission prompts, enrich the body with the concrete tool call
# Claude is asking about -- read the last tool_use from the transcript.
if [[ "$NOTIF_TYPE" == "permission_prompt" && -n "$TRANSCRIPT_PATH" && -f "$TRANSCRIPT_PATH" ]]; then
  LAST_TOOL=$(tail -n 200 "$TRANSCRIPT_PATH" 2>/dev/null | jq -cR '
      fromjson?
      | select(type == "object" and .type == "assistant")
      | .message.content[]?
      | select(.type == "tool_use")
      | {name, input}' 2>/dev/null | tail -n 1 || true)
  if [[ -n "$LAST_TOOL" ]]; then
    TOOL_NAME=$(echo "$LAST_TOOL" | jq -r '.name' 2>/dev/null || true)
    TOOL_PREVIEW=$(echo "$LAST_TOOL" | jq -r '
        .input.command
        // .input.file_path
        // .input.path
        // .input.url
        // .input.pattern
        // (.input | tostring)' 2>/dev/null | tr '\n' ' ' | cut -c1-140)
    if [[ -n "$TOOL_NAME" ]]; then
      BODY="${TOOL_NAME}: ${TOOL_PREVIEW}"
    fi
  fi
fi

# Fire webhook; HA blueprint picks it up and pushes the actionable
# notification. We capture the HTTP status so mysterious "no push on
# phone" issues can be diagnosed from notify.log without re-triggering.
HTTP_CODE=$(curl -sS -m 5 -X POST "${HA_URL%/}/api/webhook/${WEBHOOK_ID}" \
  -H "Content-Type: application/json" \
  -o /dev/null -w '%{http_code}' \
  -d "$(jq -n \
        --arg title "$TITLE" \
        --arg message "$BODY" \
        --arg tag "$TAG" \
        --arg token "$TOKEN" \
        --arg event "$EVENT" \
        '{title: $title, message: $message, tag: $tag, token: $token, event: $event}')" \
  2>/dev/null || echo "ERR")
printf '%s POST webhook -> %s\n\n' "$(date -u +%FT%TZ)" "$HTTP_CODE" \
  >> "$NOTIFY_LOG" 2>/dev/null || true
