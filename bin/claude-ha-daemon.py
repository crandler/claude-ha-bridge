#!/usr/bin/env python3
"""Claude <-> Home Assistant bridge daemon.

Subscribes to HA's WebSocket API for `mobile_app_notification_action` events.
When an action event arrives whose `tag` matches a registered Claude session,
dispatches the chosen reply into the corresponding tmux pane via `send-keys`.

No reverse SSH, no inbound ports -- the daemon connects outbound to HA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:
    sys.stderr.write(
        "aiohttp missing -- run: /usr/bin/python3 -m pip install --user aiohttp\n"
    )
    sys.exit(1)

CONFIG_DIR = Path.home() / ".config" / "claude-ha-bridge"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
LOG_FILE = CONFIG_DIR / "daemon.log"

# launchd redirects stderr into daemon.log too, so a StreamHandler on
# stderr would duplicate every line. File handler only. Restrict the log
# file permissions (and any other files we open from here on) to the
# current user -- the log contains session ids, pane targets, HA event
# payloads, and must not be world-readable on shared macOS accounts.
os.umask(0o077)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_LOG_HANDLER = logging.FileHandler(LOG_FILE)
try:
    os.chmod(LOG_FILE, 0o600)
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[_LOG_HANDLER],
)
_LOG = logging.getLogger("claude-ha-bridge")

# Ignore button presses for sessions whose registration file is older than
# this -- old notifications tapped much later should not inject keys into
# whatever happens to run in the pane now. The same threshold is the
# unconditional time-based cutoff for the cleanup loop.
SESSION_MAX_AGE_S = 600

# How often the cleanup loop scans the sessions directory.
CLEANUP_INTERVAL_S = 30

# Minimum age before we trust "prompt no longer visible" as a signal. The
# Claude Code UI can take a moment to render, so an immediate pane check
# right after a notification would race with it.
PROMPT_GONE_GRACE_S = 15

# Matches numbered prompt options Claude renders, e.g. " 1. Yes" or
# "> 1. Yes" when the Claude TUI highlights the selected row with a caret.
_OPTION_LINE = re.compile(r"^\s*[>❯]?\s*(\d+)\.\s")

# How many lines to scan upward from the bottom of the pane when looking
# for a prompt's option block. Claude renders the block at the very end,
# so we don't need to go far -- but we allow a few non-matching lines
# (cursor, input marker) between options without bailing.
_PANE_SCAN_LINES = 15
_PANE_NONMATCH_TOLERANCE = 3

# Action prefixes the blueprint emits. Matching is explicit so tags
# containing underscores never collide with the action name.
KNOWN_ACTIONS = ("approve", "allowalways", "deny", "stop")

# One-shot auth token carried by each action button. Must match what
# `notify.sh` wrote into the session file exactly: 32 hex characters,
# generated via `openssl rand -hex 16`.
_VALID_TOKEN = re.compile(r"^[a-f0-9]{32}$")


def detect_max_option(tmux_target: str) -> int | None:
    """Read the Claude pane and return the highest visible option number.

    Handles both the 2-option prompt (Yes/No) and the 3-option prompt
    (Yes / Yes-and-don't-ask-again / No). Returns None if no numbered
    block is visible.
    """
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", tmux_target],
            check=True,
            timeout=5,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as err:
        _LOG.warning("capture-pane failed for %s: %s", tmux_target, err)
        return None

    # Scan from the bottom; collect numbered lines and tolerate a few
    # non-matching lines between them (cursor row, empty line, prompt
    # caption). Bail once we pass the tolerance window after the block.
    options: list[int] = []
    nonmatch_after_block = 0
    for line in reversed(out.splitlines()[-_PANE_SCAN_LINES:]):
        m = _OPTION_LINE.match(line)
        if m:
            options.append(int(m.group(1)))
            nonmatch_after_block = 0
        elif options:
            nonmatch_after_block += 1
            if nonmatch_after_block > _PANE_NONMATCH_TOLERANCE:
                break
    # Plausibility guard: Claude's prompts cap at 3 options. Anything
    # larger is almost certainly an unrelated numbered list elsewhere.
    if not options or max(options) > 5:
        return None
    return max(options)


def resolve_keys(action: str, max_option: int | None, overrides: dict[str, str]) -> str | None:
    """Pick the key sequence to send for a button, given the live prompt shape.

    Overrides from `config.json` take precedence so power users can remap
    any button to a literal key string. Otherwise:
    - approve      -> option 1
    - allowalways  -> option 2 (only if 3+ options; falls back to option 1)
    - deny         -> last option (2 or 3)
    - stop         -> Ctrl-C
    """
    if action in overrides:
        return overrides[action]
    mo = max_option or 2
    if action == "approve":
        return "1\n"
    if action == "allowalways":
        return "2\n" if mo >= 3 else "1\n"
    if action == "deny":
        return f"{mo}\n"
    if action == "stop":
        return "\x03"
    return None


def load_config() -> dict[str, Any]:
    """Load daemon config -- HA URL, long-lived token, custom action map."""
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"Config missing: {CONFIG_FILE}\nRun install.sh first."
        )
    with CONFIG_FILE.open() as f:
        cfg = json.load(f)
    for key in ("ha_url", "ha_token"):
        if not cfg.get(key):
            raise SystemExit(f"Config field '{key}' missing in {CONFIG_FILE}")
    cfg.setdefault("actions", {})
    return cfg


def find_session_by_token(token: str) -> tuple[Path, dict[str, Any]] | None:
    """Return the (path, session) pair whose stored token matches.

    Scans SESSIONS_DIR -- typically 0-2 files at a time. An unknown token
    (from a replayed or forged event) returns None and the daemon drops
    the action silently.
    """
    if not _VALID_TOKEN.match(token):
        return None
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            age = time.time() - path.stat().st_mtime
            if age > SESSION_MAX_AGE_S:
                continue
            with path.open() as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if session.get("token") == token:
            return path, session
    return None


def dispatch_to_tmux(session: dict[str, Any], keys: str) -> bool:
    """Send keys to the tmux pane registered for a Claude session."""
    target = session.get("tmux_target")
    if not target:
        _LOG.warning("Session has no tmux_target: %s", session)
        return False
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, keys],
            check=True,
            timeout=5,
        )
        _LOG.info("Dispatched %r to %s", keys, target)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as err:
        _LOG.error("tmux send-keys failed for %s: %s", target, err)
        return False


async def discover_notify_service(
    http: aiohttp.ClientSession, cfg: dict[str, Any]
) -> str | None:
    """Return the first `notify.mobile_app_*` service HA exposes, if any."""
    url = f"{cfg['ha_url'].rstrip('/')}/api/services"
    headers = {"Authorization": f"Bearer {cfg['ha_token']}"}
    try:
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            for domain_entry in await resp.json():
                if domain_entry.get("domain") != "notify":
                    continue
                for svc_name in domain_entry.get("services", {}):
                    if svc_name.startswith("mobile_app_"):
                        return f"notify.{svc_name}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOG.warning("Service discovery failed: %s", err)
    return None


async def clear_notification(
    http: aiohttp.ClientSession, cfg: dict[str, Any], tag: str
) -> None:
    """Tell HA to retract any outstanding push with this tag."""
    svc = cfg.get("mobile_app_service")
    if not svc:
        return
    _, _, svc_name = svc.partition(".")
    url = f"{cfg['ha_url'].rstrip('/')}/api/services/notify/{svc_name}"
    headers = {"Authorization": f"Bearer {cfg['ha_token']}"}
    body = {"message": "clear_notification", "data": {"tag": tag}}
    try:
        async with http.post(
            url, headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status >= 300:
                _LOG.warning("Clear notification %s failed: %s", tag, resp.status)
            else:
                _LOG.info("Cleared notification tag=%s", tag)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOG.warning("Clear notification %s failed: %s", tag, err)


async def cleanup_stale_sessions(
    http: aiohttp.ClientSession, cfg: dict[str, Any]
) -> None:
    """Retract phone notifications whose Claude prompt is no longer relevant.

    A session is considered stale when either
    - the numbered prompt is no longer visible in the tmux pane (the user
      responded via the console, or Claude moved on), or
    - the session file is older than SESSION_MAX_AGE_S seconds
      (fallback for panes we cannot inspect).
    """
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_S)
            now = time.time()
            for path in SESSIONS_DIR.glob("*.json"):
                try:
                    age = now - path.stat().st_mtime
                    session = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue

                tag = path.stem
                reason = None
                if age > SESSION_MAX_AGE_S:
                    reason = f"age={int(age)}s"
                elif age > PROMPT_GONE_GRACE_S:
                    target = session.get("tmux_target")
                    if target and detect_max_option(target) is None:
                        reason = "prompt no longer visible in pane"

                if not reason:
                    continue

                _LOG.info("Retracting notification %s (%s)", tag, reason)
                await clear_notification(http, cfg, tag)
                try:
                    path.unlink()
                except OSError as err:
                    _LOG.warning("Failed to unlink stale session %s: %s", tag, err)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Cleanup pass failed -- continuing")


async def handle_action_event(
    cfg: dict[str, Any],
    event: dict[str, Any],
    http: aiohttp.ClientSession | None = None,
) -> None:
    """Process one actionable-notification event from HA.

    Android/cross-platform fires `mobile_app_notification_action`, iOS fires
    `ios.action_fired`. Payload shape differs slightly -- we normalise both.
    """
    data = event.get("data", {})
    action = data.get("action") or data.get("actionName")
    # iOS Companion strips per-action custom data, so the blueprint
    # encodes the one-shot token into the action name as `<known>_<token>`.
    # Split the prefix off so tokens are matched exactly, not by loose
    # string contains checks.
    token = None
    if action:
        for known in KNOWN_ACTIONS:
            prefix = f"{known}_"
            if action.startswith(prefix):
                token = action[len(prefix):]
                action = known
                break
    if not action or action not in KNOWN_ACTIONS or not token:
        return

    found = find_session_by_token(token)
    if found is None:
        # Unknown / stale / replayed token -- the only defence the daemon
        # has against spoofed button events.
        _LOG.info("Dropping action %s: token did not match any session", action)
        return
    session_path, session = found
    tag = session_path.stem

    target = session.get("tmux_target")
    max_option = detect_max_option(target) if target else None
    keys = resolve_keys(action, max_option, cfg["actions"])
    if keys is None:
        _LOG.debug("Unknown action %r, skipping", action)
        return

    _LOG.info(
        "Action %s (max_option=%s) -> session %s (%s)",
        action, max_option, tag, target,
    )
    if dispatch_to_tmux(session, keys) and http is not None:
        # Button was handled -- retract the push from the phone and
        # delete the session file so the token burns with it. A replayed
        # event with the same token will find no match.
        await clear_notification(http, cfg, tag)
        try:
            session_path.unlink()
        except OSError:
            pass


def _derive_ws_url(ha_url: str) -> str:
    """Turn `https://ha.example/` into `wss://ha.example/api/websocket`.

    Scheme-aware so hosts that themselves contain the substring `http`
    cannot be accidentally rewritten.
    """
    scheme, sep, rest = ha_url.rstrip("/").partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        raise SystemExit(f"Invalid ha_url: {ha_url!r}")
    ws_scheme = "wss" if scheme.lower() == "https" else "ws"
    return f"{ws_scheme}://{rest}/api/websocket"


async def run(cfg: dict[str, Any]) -> None:
    """Main event loop: discover notify service, start cleanup, run WebSocket."""
    ws_url = _derive_ws_url(cfg["ha_url"])

    async with aiohttp.ClientSession() as http:
        if not cfg.get("mobile_app_service"):
            cfg["mobile_app_service"] = await discover_notify_service(http, cfg)
            if cfg["mobile_app_service"]:
                _LOG.info("Discovered notify service: %s", cfg["mobile_app_service"])
            else:
                _LOG.warning(
                    "No notify.mobile_app_* service found -- "
                    "notifications cannot be cleared from the phone"
                )

        cleanup_task = asyncio.create_task(cleanup_stale_sessions(http, cfg))
        try:
            await _ws_loop(http, cfg, ws_url)
        finally:
            cleanup_task.cancel()


async def _ws_loop(
    http: aiohttp.ClientSession, cfg: dict[str, Any], ws_url: str
) -> None:
    backoff = 2
    msg_id = 1
    while True:
        try:
            _LOG.info("Connecting to %s", ws_url)
            async with http.ws_connect(ws_url, heartbeat=30) as ws:
                auth_required = await ws.receive_json()
                if auth_required.get("type") != "auth_required":
                    _LOG.error("Unexpected handshake: %s", auth_required)
                    continue

                await ws.send_json({"type": "auth", "access_token": cfg["ha_token"]})
                auth_result = await ws.receive_json()
                if auth_result.get("type") != "auth_ok":
                    _LOG.error("Auth failed: %s", auth_result)
                    raise SystemExit(1)

                _LOG.info("Authenticated, subscribing to events")
                subscribe_ok = True
                for event_type in (
                    "mobile_app_notification_action",
                    "ios.action_fired",
                ):
                    await ws.send_json({
                        "id": msg_id,
                        "type": "subscribe_events",
                        "event_type": event_type,
                    })
                    msg_id += 1
                    sub_result = await ws.receive_json()
                    if not sub_result.get("success"):
                        _LOG.error(
                            "Subscribe %s failed: %s -- reconnecting",
                            event_type, sub_result,
                        )
                        subscribe_ok = False
                        break
                    _LOG.info("Subscribed to %s", event_type)
                if not subscribe_ok:
                    # Break out of the websocket context; outer while True
                    # will reconnect with backoff.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                backoff = 2
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(msg.data)
                    if payload.get("type") != "event":
                        continue
                    event = payload.get("event", {})
                    _LOG.info(
                        "Event %s data=%s",
                        event.get("event_type"),
                        json.dumps(event.get("data", {}))[:400],
                    )
                    await handle_action_event(cfg, event, http)

        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as err:
            _LOG.warning("Connection dropped: %s -- retry in %ds", err, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _supervise(cfg: dict[str, Any]) -> None:
    """Run the websocket loop and cancel it cleanly on SIGINT/SIGTERM."""
    task = asyncio.create_task(run(cfg))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        _LOG.info("Shutdown requested, exiting")


def main() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    try:
        asyncio.run(_supervise(cfg))
    except SystemExit:
        raise
    except Exception as err:
        _LOG.exception("Fatal: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
