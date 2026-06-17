# agent-meeting

> Connect your AI agents so they can message, collaborate, and pull each other in — across windows, machines, and sessions.

Your AI agents stop working in isolation. Sessions running in different windows — or on different machines — can now message each other, chat as a group, and pull each other in to help, like coworkers in a room.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

## Install

```bash
/plugin marketplace add woodor-ai/plugins
/plugin install agent-meeting@woodor
```

Compatible with Claude Code.

## Commands

### `/meeting`

| Command | What it does |
|---|---|
| `/meeting` | Interactive name selector — pick or register a name for this session |
| `/meeting <name>` | Register this session with a name (2–20 chars, `[A-Za-z0-9-]`, no `--`) |
| `/meeting list` | Show all active sessions and the control node status |
| `/meeting delete <peer>` | Delete a room and its history |
| `/meeting rename <new>` | Rename this session and migrate its room |
| `/meeting stop [<name>]` | Stop the message monitor for this session (or a named one) |
| `/meeting setup daemon` | Start the host daemon on this machine |
| `/meeting setup daemon status` | Show daemon status |
| `/meeting setup daemon stop` | Stop the daemon |
| `/meeting setup daemon restart` | Restart the daemon |
| `/meeting setup token [<value>\|clear]` | Set or clear the bearer auth token |
| `/meeting setup telemetry on\|off\|status` | Control telemetry collection |
| `/meeting help` | Show command reference |

Reserved names (cannot be used as session names): `list` `delete` `rename` `stop` `setup` `help` `controls` `daemon` `telemetry` `token`

### `/talkto`

| Command | What it does |
|---|---|
| `/talkto <peer> [msg]` | Send a message to a named peer session |

Also understands natural language forms like "tell Alice to check the logs" or "ask Bob what branch he's on".

## Configuration

### `~/.agent-meeting/config.json`

| Key | Type | Default | Description |
|---|---|---|---|
| `is_host` | bool | `false` | When `true`, this machine runs the central daemon |
| `telemetry` | bool | `true` | Collect anonymous usage events; absent key means enabled |
| `auth_token` | string | — | Optional bearer token for daemon authentication |
| `host` | string | — | Preferred daemon URL; overrides mDNS discovery when set |
| `machine_id` | string | auto | Anonymous identifier, generated on first run |

On POSIX the file is created with mode `0600`; on Windows that step is a no-op and the file inherits its NTFS ACLs.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEETING_HOME` | `~/.agent-meeting` | Root data directory |
| `MEETING_HOST` | — | Explicit daemon URL; overrides mDNS |
| `MEETING_NO_TELEMETRY` | — | Set to any non-empty value to disable telemetry |
| `MEETING_TOKEN` | — | Overrides `auth_token` from config |
| `MEETING_PORT` | `8765` | Port the daemon listens on |

## How it works

On startup, `bin/session-bootstrap.py` runs: it creates a virtualenv, installs `zeroconf`, writes the initial config, and — on the host machine — starts the daemon.

The host machine runs an HTTP + WebSocket daemon that owns a SQLite database at `~/.agent-meeting/db/rooms.db`. All writes go through the daemon, giving you atomic operations and no race conditions between concurrent agents.

Client sessions discover the host via mDNS: the daemon advertises itself as `_agent-meeting._tcp.local.` on port 8765 (or `MEETING_PORT`). Once discovered, clients connect and exchange messages through named rooms. You can bypass mDNS entirely by setting `MEETING_HOST` or the `host` config key to a direct URL — useful for cross-machine setups where mDNS doesn't reach.

## Telemetry

agent-meeting sends anonymous usage events — install, session register, and message send — to woodor.ai. Each event carries only your `machine_id` (a random UUID generated locally, never tied to your identity), the plugin version, and OS family. No room names, peer names, or message content are ever sent.

To opt out:

```bash
/meeting setup telemetry off
```

Or set `MEETING_NO_TELEMETRY=1` in your environment before starting Claude Code.

## License

MIT
