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

The action -> key mapping is computed in `resolve_keys()` in
`bin/claude-ha-daemon.py` (approve/allowalways/deny/stop) and can be
overridden per-button via the `actions` field in `config.json`.
Override values are checked against a small whitelist of plausible
answer keys.

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

- `config.json` holds a long-lived HA token -- stored with mode `600`.
- The webhook trigger is `local_only: true` by default; flip it to
  `false` only for remote-triggered setups and add HMAC then.
- Every push carries a one-shot 128-bit token. Button events are only
  acted on when the token matches the exact session that requested the
  prompt; replayed or forged events are dropped silently.
- The `tag` on the push is used only for iOS grouping and is not
  trusted for routing.
- The session file containing the token is deleted on first successful
  dispatch, so the token burns with it.
- Routing tags are whitelisted (`^[A-Za-z0-9_-]{1,64}$`) in `notify.sh`
  before the session file is written, and the daemon enumerates session
  files via `Path.glob("*.json")` directly under `sessions/` -- both
  defuse path-traversal / symlink escapes.
- `daemon.log`, `notify.log` and session files are written with mode
  `600` (umask 077 + explicit chmod).
- Daemon only acts on known actions (`approve`, `allowalways`, `deny`,
  `stop`); unknown button events are logged and ignored.
- No inbound ports on the Mac; WebSocket is outbound TLS.

## Changelog

### 2.1.4 - 2026-04-19
- **UX:** `tap_url` default switched from the `https://claude.ai/code`
  Universal Link to the `claude://` custom URL scheme. The Universal
  Link opened the iOS app but rendered Code as an in-app WebView; the
  custom scheme opens the app natively. iOS shows a one-time cross-app
  confirmation prompt -- choose "Always Allow" to suppress it for
  future launches. The Universal Link remains a documented fallback
  inside the input description for users who prefer prompt-free
  WebView behaviour.

### 2.1.3 - 2026-04-19
- **UX:** notifications now carry a tap URL pointing to the Claude iOS
  app (`https://claude.ai/code` Universal Link by default,
  configurable via the new `tap_url` blueprint input). A short tap on
  the push opens the app straight into the Remote Control Code view;
  long-press still surfaces the action buttons.
- **UX:** action buttons are now conditional on `notification_type`.
  Permission prompts keep the full Approve/Always/Deny/Stop set so you
  can decide from the lock screen. Idle prompts, inline questions and
  any other waiting state get a slim push with a configurable
  help-needed body (`help_message` input, default "Claude benötigt
  Deine Unterstützung") and only the Stop button as a kill switch.
- **Hook:** `notify.sh` includes the `notification_type` field in the
  webhook payload so the blueprint can branch on it without
  re-inspecting the transcript.
- **Upgrade:** re-import blueprint v2.1.3 and re-save the automation;
  the new `tap_url` and `help_message` inputs accept their defaults
  silently if you don't set them.

### 2.1.2 - 2026-04-19
- **Reliability:** LaunchAgent `KeepAlive` no longer restarts the daemon
  on a regular non-zero exit; the auth-failure SystemExit(1) was being
  defeated by the previous `SuccessfulExit: false` clause. Restart now
  happens only on signal-style crashes; the daemon's own log message
  tells you to `launchctl kickstart` after fixing the underlying issue.
- **Security:** `notify.sh` now whitelists the routing tag against
  `^[A-Za-z0-9_-]{1,64}$` before writing the session file -- both real
  sources match by construction, but defense-in-depth against an
  unexpected `session_id` shape from upstream.
- **UX:** `notify.sh` skips the webhook entirely when not running inside
  tmux. Without a tmux pane the daemon could never route a button tap,
  so previous installs would surface a push whose buttons all silently
  dropped on tap.
- **Reliability:** `notify.log` rotates at 1 MB with a single `.1`
  backup, mirroring the daemon log.
- **Docs:** README correctly references `resolve_keys()` instead of the
  long-removed `DEFAULT_ACTIONS` constant, and the tag-whitelist
  security note now matches the actual code path.

### 2.1.1 - 2026-04-19
- **UX:** permission-prompt push body no longer dumps raw tool `input`
  JSON when the tool lacks a `command`/`file_path`/`path`/`url`/`pattern`
  field. The notify hook now also reads `query`, `description`, `prompt`,
  `subagent_type`, `skill` and falls back to showing just the tool name
  instead of an unreadable JSON blob for tools like `TaskUpdate` or
  `TodoWrite`.

### 2.1.0 - 2026-04-19
- **Reliability:** daemon gives up after 5 successive auth failures
  with a clear "check ha_token" hint, instead of letting launchd
  throttle-loop forever. Backoff and auth-fail counters reset
  immediately on `auth_ok`; `msg_id` resets per connection.
- **Reliability:** cleanup task is awaited after cancel on shutdown so
  no `ResourceWarning` leaks from in-flight HTTP requests.
- **Reliability:** daemon.log now rotates at 1 MB with 3 backups --
  long-running installs cannot silently fill the disk.
- **Security (S5):** `config.json` action overrides are validated
  against a whitelist of plausible answer keys. A tampered config can
  no longer slip arbitrary keystrokes into the Claude pane.
- **Security (S6):** installer warns loudly when the HA URL is plain
  `http://` -- the long-lived token would otherwise travel unencrypted
  on every reconnect.
- **Robustness (B6):** `handle_action_event` re-checks the pane right
  before dispatch. If the prompt disappeared between token lookup and
  `send-keys` (user answered in the terminal meanwhile), no keys are
  injected and the push is retracted.
- **Robustness:** `send-keys` uses tmux named keys (`C-c`, `C-d`) for
  control characters so interpretation is mode-independent.
- **Robustness:** pane scan widened from 15 to 25 lines to cover
  wrapped option titles at 80-col terminals; plausibility cap
  relaxed from 5 to 9.
- **Privacy:** full HA event payloads are logged at DEBUG, not INFO.
- **Hook:** atomic session-file write via `mktemp + mv`. HTTP status of
  the webhook POST is appended to `notify.log` for diagnostics. Tag
  fallback switched from SHA-1 to SHA-256.

### 2.0.0 - 2026-04-19 (breaking)
- **Security (S1):** notify.sh now generates a fresh 128-bit one-shot
  token per push. The Blueprint encodes it into each action button and
  the daemon routes events by token, not by tag. Replayed or forged
  button events without a matching session file are dropped.
  - *Upgrade:* re-import Blueprint v2.0.0 and re-save the automation.
    Running notify.sh on an older daemon, or vice versa, will not
    authorise any buttons.
- **Security (S2):** Tag whitelist (`^[A-Za-z0-9_-]{1,64}$`) plus
  resolved-path check under `sessions/` -- replay events can no longer
  coerce the daemon into deleting `.json` files outside the sessions
  directory.
- **Security (S3):** `daemon.log`, `notify.log` and per-session files
  are now written with mode 600 (umask + chmod).
- **Security (S4):** Blueprint webhook trigger defaults to
  `local_only: true`. Remote-triggered setups must flip this manually
  and should add HMAC protection.
- **Bugfix (B1):** Action-name split now uses a known-prefix list
  instead of `partition("_")`, so tags that contain underscores are
  preserved.
- **Bugfix (B2):** Websocket URL is derived via scheme partition and
  rejects non-http(s) inputs at startup -- `replace("http","ws")` was
  fragile on upper-case schemes and hosts that contain `http`.
- **Bugfix (B3):** A failed `subscribe_events` response now breaks out
  of the websocket context so the reconnect loop retries with backoff
  instead of sitting idle without any subscriptions.
- **Bugfix (B5):** `detect_max_option` tolerates a leading `>`/`❯`
  highlight on the selected row, allows up to 3 non-matching lines
  (cursor, input marker) between option lines, and rejects numbered
  blocks with max > 5 so unrelated enumerations are not misread as a
  prompt.

### 1.8.1 - 2026-04-19
- Daemon: retract pushes as soon as the Claude prompt is no longer
  visible in the tmux pane -- e.g. when you respond in the terminal
  instead of on the phone. The 10-minute absolute age cutoff still
  applies as a fallback. Cleanup interval tightened to 30s with a 15s
  grace period after registration to avoid races with TUI render.

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
