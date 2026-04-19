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
# stderr would duplicate every line. File handler only.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)],
)
_LOG = logging.getLogger("claude-ha-bridge")

# Ignore button presses for sessions whose registration file is older than
# this -- old notifications tapped much later should not inject keys into
# whatever happens to run in the pane now.
SESSION_MAX_AGE_S = 600

# Matches numbered prompt options Claude renders, e.g. " 1. Yes".
_OPTION_LINE = re.compile(r"^\s*(\d+)\.\s")


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

    # Scan from the bottom for the last contiguous block of "N. ..." lines.
    options: list[int] = []
    for line in reversed(out.splitlines()[-40:]):
        m = _OPTION_LINE.match(line)
        if m:
            options.append(int(m.group(1)))
        elif options:
            break
    return max(options) if options else None


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


def load_session(tag: str) -> dict[str, Any] | None:
    """Load tmux target for a Claude session tag, ignoring stale entries."""
    path = SESSIONS_DIR / f"{tag}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > SESSION_MAX_AGE_S:
        _LOG.info("Session %s stale (age=%ds), ignoring", tag, int(age))
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        _LOG.warning("Failed to read session %s: %s", tag, err)
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


async def handle_action_event(cfg: dict[str, Any], event: dict[str, Any]) -> None:
    """Process one actionable-notification event from HA.

    Android/cross-platform fires `mobile_app_notification_action`, iOS fires
    `ios.action_fired`. Payload shape differs slightly -- we normalise both.
    """
    data = event.get("data", {})
    # mobile_app: {"action": "...", "tag": "..."}
    # ios.action_fired: {"actionName": "...", "action_data": {"tag": "..."}}
    action = data.get("action") or data.get("actionName")
    tag = data.get("tag")
    if not tag:
        action_data = data.get("action_data") or {}
        if isinstance(action_data, dict):
            tag = action_data.get("tag")
    # iOS Companion strips per-action custom data, so the blueprint encodes
    # the tag into the action name as `<name>_<tag>`. Split it back out.
    if action and "_" in action and not tag:
        action, _, tag = action.partition("_")
    if not action or not tag:
        return

    session = load_session(tag)
    if session is None:
        _LOG.debug("No session registered for tag %r", tag)
        return

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
    dispatch_to_tmux(session, keys)


async def run(cfg: dict[str, Any]) -> None:
    """Main WebSocket loop with reconnect."""
    ws_url = cfg["ha_url"].rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    backoff = 2
    msg_id = 1

    async with aiohttp.ClientSession() as http:
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
                                "Subscribe %s failed: %s", event_type, sub_result
                            )
                            continue
                        _LOG.info("Subscribed to %s", event_type)

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
                        await handle_action_event(cfg, event)

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
