#!/usr/bin/env bash
# One-liner bootstrap. Clones claude-ha-bridge to a fixed location
# (or updates an existing checkout) and hands off to install.sh.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/crandler/claude-ha-bridge/main/bootstrap.sh)"
#
# Override the checkout location by exporting CLAUDE_HA_BRIDGE_DIR
# before invoking the one-liner.

set -euo pipefail

REPO_URL="https://github.com/crandler/claude-ha-bridge.git"
INSTALL_DIR="${CLAUDE_HA_BRIDGE_DIR:-${HOME}/.claude-ha-bridge}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

command -v git  >/dev/null 2>&1 || die "git not found -- install via Homebrew"
command -v curl >/dev/null 2>&1 || die "curl not found"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating existing checkout at ${INSTALL_DIR}"
  git -C "${INSTALL_DIR}" pull --ff-only \
    || die "git pull --ff-only failed in ${INSTALL_DIR}"
elif [[ -e "${INSTALL_DIR}" ]]; then
  die "${INSTALL_DIR} exists but is not a git checkout -- remove it or set CLAUDE_HA_BRIDGE_DIR"
else
  log "Cloning ${REPO_URL} into ${INSTALL_DIR}"
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

exec "${INSTALL_DIR}/install.sh"
