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
3. Create an automation from the blueprint, set:
   - `notify_device` = pick your phone from the device dropdown
   - `webhook_id` = the value the wizard showed you

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
