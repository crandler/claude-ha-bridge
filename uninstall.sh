#!/usr/bin/env bash
# Uninstall claude-ha-bridge.
#
# One-liner:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/crandler/claude-ha-bridge/main/uninstall.sh)"
#
# Removes the LaunchAgent + its plist, the runtime state in
# ~/.config/claude-ha-bridge (including the HA long-lived token in
# config.json), and the bootstrap checkout at ~/.claude-ha-bridge
# (override via CLAUDE_HA_BRIDGE_DIR). Leaves custom checkout paths
# alone unless they look like a claude-ha-bridge repo.
#
# Pass --yes / -y to skip confirmation.
# Cannot remove the Claude Code hook entry or HA automation/blueprint;
# both are flagged as manual steps at the end.

set -euo pipefail

LABEL="com.crandler.claude-ha-bridge"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
CONFIG_DIR="${HOME}/.config/claude-ha-bridge"
REPO_DIR="${CLAUDE_HA_BRIDGE_DIR:-${HOME}/.claude-ha-bridge}"
UID_NUM="$(id -u)"

assume_yes=""
for arg in "$@"; do
  case "${arg}" in
    -y|--yes) assume_yes=1 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }

cat <<EOF
claude-ha-bridge uninstaller

Will remove:
  - LaunchAgent:  gui/${UID_NUM}/${LABEL}
  - plist:        ${PLIST}
  - config dir:   ${CONFIG_DIR}  (contains the HA long-lived token)
  - repo clone:   ${REPO_DIR}    (only if it is a claude-ha-bridge checkout)

Not touched (manual cleanup required):
  - Claude Code hook entry in ~/.claude/settings.json
  - Home Assistant automation and imported blueprint
EOF

if [[ -z "${assume_yes}" ]]; then
  read -r -p "Proceed? [y/N] " answer </dev/tty
  case "${answer}" in
    y|Y|yes|YES) ;;
    *) warn "aborted"; exit 1 ;;
  esac
fi

if launchctl print "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1; then
  log "Stopping LaunchAgent"
  launchctl bootout "gui/${UID_NUM}/${LABEL}" || true
else
  log "LaunchAgent not loaded -- nothing to stop"
fi

if [[ -f "${PLIST}" ]]; then
  log "Removing ${PLIST}"
  rm -f "${PLIST}"
fi

if [[ -d "${CONFIG_DIR}" ]]; then
  log "Removing ${CONFIG_DIR}"
  rm -rf "${CONFIG_DIR}"
fi

# Only touch the repo dir if it actually looks like this project, so a
# user who pointed CLAUDE_HA_BRIDGE_DIR at something unexpected does
# not lose unrelated files.
if [[ -d "${REPO_DIR}" ]]; then
  if [[ -f "${REPO_DIR}/bin/claude-ha-daemon.py" && -f "${REPO_DIR}/install.sh" ]]; then
    log "Removing repo clone ${REPO_DIR}"
    rm -rf "${REPO_DIR}"
  else
    warn "${REPO_DIR} does not look like a claude-ha-bridge checkout -- leaving untouched"
  fi
fi

cat <<EOF

Done. Remaining manual steps:
  1. Edit ~/.claude/settings.json and remove the claude-ha-bridge hook
     entry (the "Notification" block pointing at notify.sh).
  2. In Home Assistant: delete the Claude HA Bridge automation and,
     if no longer used, the imported blueprint.
EOF
