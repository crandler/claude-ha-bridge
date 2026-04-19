# claude-ha-bridge

Push Claude Code permission prompts from your Mac to your phone as
**actionable notifications** via Home Assistant. Tap a button on the lock
screen, and the answer lands in the correct `tmux` pane back on the Mac.

No reverse SSH, no open ports, no cloud account -- the Mac daemon opens an
outbound WebSocket to your own HA instance and that's it.

## Architecture

```
 Claude Code (hook)
         |  stdin: hook JSON
         v
 hooks/notify.sh  ---- HTTPS POST (webhook) ---->  Home Assistant
                                                        |
                                                Blueprint -> mobile_app
                                                        |
                                                        v
                                                    iPhone
                                                        |
                                                tap button
                                                        v
                                      mobile_app_notification_action event
                                                        |
                                    HA WebSocket <------+
                                        ^
                                        | outbound WS
 bin/claude-ha-daemon.py  <--------------+
         |
         v
 tmux send-keys  -->  target Claude pane
```

Why this shape

- **Outbound only**: The Mac connects out to HA; nothing listens for inbound
  traffic. Works behind NAT, no router config.
- **Routing by tag**: Each Claude session registers its tmux target in
  `~/.config/claude-ha-bridge/sessions/<tag>.json`. Both webhook and push
  notification carry the same `tag`, so the button answer finds its pane
  even when several Claude sessions run in parallel.
- **Stateless HA side**: HA just fans webhook -> push. All routing state
  lives on the Mac.

## Repo layout

```
bin/claude-ha-daemon.py              # WebSocket listener, dispatches to tmux
hooks/notify.sh                      # Claude Code Notification hook
ha/claude-ha-bridge.yaml             # Home Assistant Blueprint
launchd/com.crandler.claude-ha-bridge.plist  # LaunchAgent template
install.sh                           # interactive installer wizard
```

## Prerequisites

- macOS (tested on Tahoe)
- Home Assistant with the **Mobile App** integration and a registered phone
- `python3`, `jq`, `tmux`, `curl`, Homebrew (for the above)
- A Home Assistant **long-lived access token**
  (HA user profile -> Security -> Long-lived access tokens)

## Install

```bash
git clone <this-repo> ~/Desktop/CODING/Privat/claude-ha-bridge
cd ~/Desktop/CODING/Privat/claude-ha-bridge
./install.sh
```

The wizard

1. creates `~/.config/claude-ha-bridge/` with a Python venv
2. asks for HA URL, token, webhook ID (generates one if empty)
3. writes `config.json` with mode `600`
4. renders `launchd/com.crandler.claude-ha-bridge.plist` into
   `~/Library/LaunchAgents/` and loads it via `launchctl bootstrap`
5. copies the Blueprint YAML to your clipboard

### Finish on the HA side

1. HA -> Settings -> Automations & Scenes -> Blueprints
2. "Import Blueprint" -> paste from clipboard -> save
3. *(optional)* Settings -> Devices & Services -> Helpers -> Create
   Helper -> Dropdown. Name it `Claude Notifications Mode`, add the
   three options `off`, `on`, `auto`. Surface it on any dashboard to
   toggle notifications from the frontend.
4. Create an automation from the blueprint, set:
   - `notify_device` = pick your phone from the device dropdown
   - `webhook_id` = the value the wizard showed you
   - `mode_entity` *(optional)* = the dropdown helper from step 3
   - `presence_entity` *(optional)* = person/device_tracker that
     reports `home`; required when `mode_entity` is set to `auto`

### Wire the Claude Code hook

Add to `~/.claude/settings.json` (merge with existing `hooks`):

```json
{
  "hooks": {
    "Notification": [{
      "matcher": "permission_prompt|idle_prompt",
      "hooks": [{
        "type": "command",
        "command": "/Users/YOU/Desktop/CODING/Privat/claude-ha-bridge/hooks/notify.sh"
      }]
    }]
  }
}
```

## How a round-trip looks

1. Claude asks for permission -> `notify.sh` registers the current tmux
   pane in `sessions/<tag>.json` and POSTs the webhook
2. HA Blueprint sends the actionable push to your phone
3. You tap "Erlauben" / "Ablehnen" / "Stoppen"
4. HA fires `mobile_app_notification_action`
5. Daemon matches `tag` -> `tmux send-keys` into the Claude pane
   (`1\n`, `2\n`, `Ctrl-C`)

The action -> key mapping lives in `bin/claude-ha-daemon.py` as
`DEFAULT_ACTIONS` and can be overridden via the `actions` field in
`config.json`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No push on phone | HA automation trace of the Blueprint, mobile_app registration |
| Push comes, button does nothing | `tail -f ~/.config/claude-ha-bridge/daemon.log`, token valid? |
| Daemon stuck reconnecting | HA URL reachable from Mac? token revoked? |
| Wrong pane receives keys | stale `sessions/<tag>.json` -- delete and re-trigger |

Daemon status:

```bash
launchctl print gui/$(id -u)/com.crandler.claude-ha-bridge | head -n 20
```

Log:

```bash
tail -f ~/.config/claude-ha-bridge/daemon.log
```

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.crandler.claude-ha-bridge
rm ~/Library/LaunchAgents/com.crandler.claude-ha-bridge.plist
rm -rf ~/.config/claude-ha-bridge
```

Remove the Claude Code hook entry from `~/.claude/settings.json` and delete
the automation/blueprint in HA.

## Security notes

- `config.json` holds a long-lived HA token -- stored with mode `600`
- Webhook uses HA's unguessable webhook ID as the only secret; rotate via
  wizard + HA automation re-save if exposed
- Daemon only acts on known actions (`approve`, `deny`, `stop`); anything
  else is logged and ignored
- No inbound ports on the Mac; WebSocket is outbound TLS

## Changelog

### 1.8.0 - 2026-04-19
- Daemon: retract stale pushes from the phone. After a button press
  (successful tmux dispatch) the daemon calls
  `notify.mobile_app_*.clear_notification` with the session tag, and a
  background task scans the sessions directory every 60s to clear any
  notification whose session is older than 10 minutes. The notify
  service is auto-discovered from HA; `config.json` key
  `mobile_app_service` overrides it.

### 1.7.0 - 2026-04-19
- Blueprint: new optional `mode_entity` (an `input_select` helper with
  the values `off` / `on` / `auto`) and `presence_entity` inputs. Off
  silences notifications; on always notifies; auto only notifies while
  the presence entity is not `home`. Unset behaves like the previous
  "always notify" default.

### 1.6.0 - 2026-04-19
- Blueprint: add fourth button `Immer erlauben` (allow-always) so the
  full Claude 3-option permission prompt is addressable.
- Daemon: resolve button -> keys dynamically. `tmux capture-pane` is
  inspected to find the highest numbered option currently on screen;
  `deny` then sends `2` or `3` depending on the prompt shape, and
  `allowalways` falls back to `approve` when only two options exist.
  `config.json "actions": {...}` still overrides individual buttons.

### 1.5.2 - 2026-04-19
- Daemon: shut down cleanly on SIGINT/SIGTERM via `task.cancel()` instead
  of `loop.stop()`. The latter raised
  `RuntimeError: Event loop stopped before Future completed` on every
  restart; now the shutdown path logs `Shutdown requested, exiting` and
  exits without a traceback.

### 1.5.1 - 2026-04-19
- Daemon: drop the stderr StreamHandler. launchd already redirects
  stderr into `daemon.log`, so the extra handler caused every log line
  to appear twice.
- Daemon: ignore button presses for sessions whose registration is
  older than 10 minutes, so late taps on stale notifications do not
  inject keys into whatever pane is active now.

### 1.5.0 - 2026-04-19
- Blueprint + daemon: encode the routing tag into the action name itself
  (`approve_<tag>`). iOS Companion App does not reflect per-action
  `data` back into the event, so the action name is the only reliable
  carrier. Daemon splits the suffix off before dispatching.

### 1.4.2 - 2026-04-19
- Blueprint: remove the version suffix from the blueprint name. A
  changing name caused HA to treat each re-import as a separate
  blueprint instead of overwriting the existing one. The version
  remains visible in the description.

### 1.4.1 - 2026-04-19
- Blueprint: attach the notification `tag` to each actionable button's
  own `data` payload. iOS strips top-level tag from the action event, so
  the daemon previously received `{"action":"approve"}` without a tag
  and could not route the reply.
- Daemon: subscribe to `ios.action_fired` in addition to
  `mobile_app_notification_action` and log every inbound event for easier
  debugging.
- `notify.sh`: survive malformed transcript lines while extracting the
  last tool-use; render permission prompts as `<tool>: <input-preview>`.

### 1.4.0 - 2026-04-19
- `notify.sh`: include a short session id in the push title so parallel
  Claude sessions are distinguishable (`Claude - project - ab12cd`).
- `notify.sh`: append the full hook payload to
  `~/.config/claude-ha-bridge/notify.log` for debugging. Contains session
  ids and project paths -- not shared externally, safe to keep local only.

### 1.3.0 - 2026-04-19
- Blueprint: add all 127 notification sounds shipped with the iOS
  Companion App (Alexa, Daisy, Morgan Freeman voice packs) as dropdown
  presets. Sourced from `home-assistant/iOS` repo at import time; custom
  filenames remain supported via `custom_value`.

### 1.2.2 - 2026-04-19
- Blueprint: surface the version in the blueprint name and description so
  re-imports are visibly confirmed in the HA UI.

### 1.2.1 - 2026-04-19
- Blueprint: quote `none`/`default` sound-selector values so YAML does not
  coerce them to `null`, which suppressed the dropdown in HA.

### 1.2.0 - 2026-04-19
- Blueprint: `notification_sound` input is now a dropdown with `Default`
  and `None (silent)` presets; custom filenames remain possible via the
  selector's `custom_value` field.

### 1.1.0 - 2026-04-19
- Blueprint: replace free-text `notify_service` input with a device selector
  filtered to the `mobile_app` integration; the notify service is derived
  from the selected device name. Re-import the blueprint and re-save the
  automation to pick up the new input.

### 1.0.0 - 2026-04-19
- Initial release: daemon, notification hook, HA Blueprint, LaunchAgent,
  installer wizard
