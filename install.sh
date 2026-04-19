#!/usr/bin/env bash
# claude-ha-bridge installer wizard.
#
# - Validates dependencies (python3, jq, tmux, curl, pbcopy)
# - Creates ~/.config/claude-ha-bridge/ with venv, installs aiohttp
# - Prompts for HA URL, token, webhook_id
# - Writes config.json with mode 600
# - Renders launchd plist from template, loads LaunchAgent
# - Copies HA Blueprint YAML to clipboard for import
# - Prints the Claude Code hook snippet for ~/.claude/settings.json

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${HOME}/.config/claude-ha-bridge"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SESSIONS_DIR="${CONFIG_DIR}/sessions"
VENV_DIR="${CONFIG_DIR}/venv"
LABEL="com.crandler.claude-ha-bridge"
PLIST_SRC="${REPO_DIR}/launchd/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
BLUEPRINT_SRC="${REPO_DIR}/ha/claude-ha-bridge.yaml"
UID_NUM="$(id -u)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1 -- install via Homebrew"
}

prompt() {
  local var="$1" label="$2" default="${3:-}" silent="${4:-}" value
  if [[ -n "$default" ]]; then
    read -r -p "${label} [${default}]: " value
    value="${value:-$default}"
  elif [[ "$silent" == "silent" ]]; then
    read -r -s -p "${label}: " value
    echo
  else
    read -r -p "${label}: " value
  fi
  printf -v "$var" '%s' "$value"
}

log "Repo: ${REPO_DIR}"

# Pull latest if this is a git checkout ----------------------------------------
# Makes install.sh double as the upgrader: `./install.sh` from the cloned
# directory fetches new commits, re-renders the plist, reloads the daemon
# and copies the latest blueprint to the clipboard -- one entry point.
# Tarball installs (no .git) skip this silently.
if [[ -d "${REPO_DIR}/.git" ]]; then
  if command -v git >/dev/null 2>&1; then
    log "Updating repo (git pull --ff-only)"
    if ! (cd "${REPO_DIR}" && git pull --ff-only); then
      warn "git pull --ff-only failed -- continuing with current working tree"
    fi
  else
    warn "git not installed -- skipping repo update"
  fi
fi

# Dependencies -----------------------------------------------------------------
log "Checking dependencies"
require python3
require jq
require curl
require tmux
require pbcopy
require launchctl

# Config directory -------------------------------------------------------------
log "Preparing ${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}" "${SESSIONS_DIR}"
chmod 700 "${CONFIG_DIR}" "${SESSIONS_DIR}"

# venv + deps ------------------------------------------------------------------
if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
  log "Creating Python venv at ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi
log "Installing aiohttp in venv"
"${VENV_DIR}/bin/python3" -m pip install --quiet --upgrade pip
"${VENV_DIR}/bin/python3" -m pip install --quiet aiohttp

# Config prompts ---------------------------------------------------------------
existing_ha_url=""
existing_webhook=""
if [[ -f "${CONFIG_FILE}" ]]; then
  log "Existing config found, re-using values as defaults"
  existing_ha_url=$(jq -r '.ha_url // ""' "${CONFIG_FILE}")
  existing_webhook=$(jq -r '.webhook_id // ""' "${CONFIG_FILE}")
fi

prompt HA_URL "Home Assistant URL (e.g. https://ha.example.com)" "${existing_ha_url}"
shopt -s nocasematch
[[ "${HA_URL}" =~ ^https?:// ]] || die "HA URL must start with http:// or https://"
if [[ "${HA_URL}" =~ ^http:// ]]; then
  warn "http:// URL given -- your long-lived HA token will travel UNENCRYPTED on every WebSocket reconnect and on every clear_notification call. Use https:// unless you are deliberately testing on an isolated LAN."
fi
shopt -u nocasematch

prompt HA_TOKEN "Long-lived access token (input hidden)" "" silent
[[ -n "${HA_TOKEN}" ]] || die "token required"

if [[ -n "${existing_webhook}" ]]; then
  prompt WEBHOOK_ID "Webhook ID" "${existing_webhook}"
else
  GENERATED_WEBHOOK="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  prompt WEBHOOK_ID "Webhook ID (leave empty to use generated)" "${GENERATED_WEBHOOK}"
fi

# config.json ------------------------------------------------------------------
# Merge into the existing config so optional fields a power user added
# manually (mobile_app_service override, per-button `actions` overrides,
# future fields) survive a re-run of the wizard.
log "Writing ${CONFIG_FILE}"
umask 077
TMP_CFG=$(mktemp "${CONFIG_DIR}/.config.XXXXXX")
if [[ -f "${CONFIG_FILE}" ]]; then
  jq \
    --arg ha_url "${HA_URL%/}" \
    --arg ha_token "${HA_TOKEN}" \
    --arg webhook_id "${WEBHOOK_ID}" \
    '. + {ha_url: $ha_url, ha_token: $ha_token, webhook_id: $webhook_id}' \
    "${CONFIG_FILE}" > "${TMP_CFG}"
else
  jq -n \
    --arg ha_url "${HA_URL%/}" \
    --arg ha_token "${HA_TOKEN}" \
    --arg webhook_id "${WEBHOOK_ID}" \
    '{ha_url: $ha_url, ha_token: $ha_token, webhook_id: $webhook_id}' \
    > "${TMP_CFG}"
fi
chmod 600 "${TMP_CFG}"
mv -f "${TMP_CFG}" "${CONFIG_FILE}"
umask 022

# launchd plist ----------------------------------------------------------------
log "Installing LaunchAgent ${LABEL}"
mkdir -p "${HOME}/Library/LaunchAgents"
sed -e "s|__HOME__|${HOME}|g" -e "s|__REPO__|${REPO_DIR}|g" "${PLIST_SRC}" > "${PLIST_DST}"
chmod 644 "${PLIST_DST}"

if launchctl print "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1; then
  log "Unloading previous LaunchAgent instance"
  launchctl bootout "gui/${UID_NUM}/${LABEL}" || true
fi
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DST}"
launchctl enable "gui/${UID_NUM}/${LABEL}" || true
log "LaunchAgent loaded -- daemon running"

# Blueprint to clipboard -------------------------------------------------------
if [[ -f "${BLUEPRINT_SRC}" ]]; then
  pbcopy < "${BLUEPRINT_SRC}"
  log "HA Blueprint copied to clipboard"
else
  warn "Blueprint file missing at ${BLUEPRINT_SRC}"
fi

# Final hints ------------------------------------------------------------------
cat <<EOF

-------------------------------------------------------------------------------
Done. Next steps:

1. Import the HA Blueprint
   - Open HA: Settings -> Automations & Scenes -> Blueprints
   - Click "Import Blueprint", paste the YAML from your clipboard, save
   - Create an automation from it, set:
       notify_device = pick your phone from the device dropdown
       webhook_id    = ${WEBHOOK_ID}

2. Wire the Claude Code hook
   Add this to ~/.claude/settings.json (merge with existing hooks):

   {
     "hooks": {
       "Notification": [{
         "matcher": "permission_prompt|idle_prompt",
         "hooks": [{
           "type": "command",
           "command": "${REPO_DIR}/hooks/notify.sh"
         }]
       }]
     }
   }

3. Watch the daemon log (Ctrl-C to detach):
   tail -f ${CONFIG_DIR}/daemon.log

Uninstall: launchctl bootout gui/${UID_NUM}/${LABEL} && rm ${PLIST_DST}
-------------------------------------------------------------------------------
EOF
