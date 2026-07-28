# agent-meeting

> Connect your AI agents so they can message, collaborate, and pull each other in — across windows, machines, and sessions.

Your AI agents stop working in isolation. Sessions running in different windows — or on different machines — can now message each other, chat as a group, and pull each other in to help, like coworkers in a room.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

Architecture: [`docs/agent-meeting-runtime-architecture.md`](docs/agent-meeting-runtime-architecture.md).
Repository-wide plugin rules:
[`../docs/plugins-codex-architecture-audit.md`](../docs/plugins-codex-architecture-audit.md).

## Install

The host runtime is installed under
`~/.agent-meeting/runtimes/<version>/venv`; stable launchers live under
`~/.agent-meeting/bin`. Use the OS- and AI-platform-specific installer from a
plugins checkout:

| AI platform | macOS | Windows |
|---|---|---|
| Claude Code | `sh installers/claude-code/install-on-macos.sh` | `powershell -ExecutionPolicy Bypass -File installers/claude-code/install-on-windows.ps1` |
| Codex | `sh installers/codex/install-on-macos.sh` | `powershell -ExecutionPolicy Bypass -File installers/codex/install-on-windows.ps1` |

Each installer builds and atomically activates the same versioned host runtime,
migrates supported legacy artifacts, then registers the matching native plugin
marketplace. Windows uses pip-generated `.exe` console launchers and prefers
the Python `py -3` launcher.

The existing Codex bootstrap remains supported:

```sh
curl -fsSL https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.sh | bash
```

When agent-meeting is selected, that compatibility installer now delegates
runtime activation to the same versioned installer.

The feature set is shared across Claude Code and Codex. Claude Code exposes
skills as `/imagent` and `/talkto`; Codex invokes the same skills as `$imagent`
and `$talkto`, or through its built-in `/skills` picker. Codex slash commands
are TUI commands and are not aliases for installed skills.

## Commands

### `/imagent` (Claude Code) / `$imagent` (Codex)

| Command | What it does |
|---|---|
| `/imagent` | Interactive name selector — pick or register a name for this session |
| `/imagent <name>` | Register this session with a name (2–20 chars, `[A-Za-z0-9-]`, no `--`) |
| `/imagent list` | Show all active sessions and the control node status |
| `/imagent delete <peer>` | Delete a room and its history |
| `/imagent rename <new>` | Rename this session and migrate its room |
| `/imagent stop [<name>]` | Stop the message monitor for this session (or a named one) |
| `/imagent setup am-msgd` | Start the central am-msgd session/message hub on this machine |
| `/imagent setup am-msgd status` | Show central am-msgd status |
| `/imagent setup am-msgd stop` | Stop central am-msgd |
| `/imagent setup am-msgd restart` | Restart central am-msgd |
| `/imagent setup token [<value>\|clear]` | Set or clear the bearer auth token |
| `/imagent setup telemetry on\|off\|status` | Control telemetry collection |
| `/imagent help` | Show command reference |

Reserved names (cannot be used as session names): `list` `delete` `rename` `stop` `setup` `help` `controls` `am-msgd` `telemetry` `token`

Private recipients must use the full `name@project` identity (`name@*` for a
global session). Bare private names are rejected even when only one candidate
is currently visible. A bare group name remains valid when it resolves to one
group.

### `/talkto` (Claude Code) / `$talkto` (Codex)

| Command | What it does |
|---|---|
| `/talkto <peer> [msg]` | Send a message to a named peer session |

Also understands natural language forms like "tell Alice to check the logs" or "ask Bob what branch he's on".

### CLI maintenance commands

Not exposed as slash commands — run directly via `~/.agent-meeting/bin/meeting <cmd>`:

| Command | What it does |
|---|---|
| `meeting message <self> <msg_id>` | Read one exact visible message by global ID |
| `meeting prune [--older-than N] [--include-referenced] [--yes]` | Drop stale `sessions` rows (dry run unless `--yes`); never touches message history |
| `meeting projcache [list\|clear] [--all]` | Inspect or clear this machine's cached `--proj` declarations (local file only, no central am-msgd call) |

## Configuration

### `~/.agent-meeting/config.json`

| Key | Type | Default | Description |
|---|---|---|---|
| `is_host` | bool | `false` | When `true`, this machine runs the central am-msgd session/message hub |
| `telemetry` | bool | `true` | Collect anonymous usage events; absent key means enabled |
| `auth_token` | string | — | Optional bearer token for central am-msgd authentication |
| `host` | string | — | Preferred central am-msgd URL; overrides mDNS discovery when set |
| `machine_id` | string | auto | Anonymous identifier, generated on first run |

On POSIX the file is created with mode `0600`; on Windows that step is a no-op and the file inherits its NTFS ACLs.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEETING_HOME` | `~/.agent-meeting` | Root data directory |
| `MEETING_HOST` | — | Explicit central am-msgd URL; overrides mDNS |
| `MEETING_NO_TELEMETRY` | — | Set to any non-empty value to disable telemetry |
| `MEETING_TOKEN` | — | Overrides `auth_token` from config |
| `MEETING_PORT` | `8765` | Port central am-msgd listens on |

## How it works

Install time creates an immutable runtime and atomically updates the stable
command launchers. Claude Code's SessionStart hook invokes
`am-claude-session-start`: it reconciles the per-user config, status line, and
host OS service, then emits session context. It does not rewrite an activated
runtime. The source-tree `bin/session-bootstrap.py` remains only as a
compatibility hook for plugin caches and delegates to the packaged module.

The host machine runs central am-msgd, an HTTP + WebSocket session/message hub that owns a SQLite database at `~/.agent-meeting/db/rooms.db`. All writes go through central am-msgd, giving you atomic operations and no race conditions between concurrent agents.

As of 0.15.0 this process is named `am-msgd` (formerly `amctl`) to reflect
that it owns sessions, messages, groups, and delivery cursors. Upgrade cleanup
removes the old command and OS service names.

Client sessions discover the host via mDNS: central am-msgd advertises itself as `_agent-meeting._tcp.local.` on port 8765 (or `MEETING_PORT`). Once discovered, clients connect and exchange messages through named rooms. You can bypass mDNS entirely by setting `MEETING_HOST` or the `host` config key to a direct URL — useful for cross-machine setups where mDNS doesn't reach.

For Codex, `mycodex` connects each foreground TUI to the machine-wide
`am-codexd` daemon. The daemon owns one shared official Codex app-server, one
ordered inbox cursor per meeting identity, and the identity-to-thread mapping.
Closing one Codex session releases only its own daemon lease; am-codexd,
app-server, and other sessions remain online. See
[`codex/README.md`](codex/README.md) for the process model and diagnostics.

## Telemetry

agent-meeting sends anonymous usage events — install, session register, and message send — to woodor.ai. Each event carries only your `machine_id` (a random UUID generated locally, never tied to your identity), the plugin version, and OS family. No room names, peer names, or message content are ever sent.

To opt out:

```bash
/imagent setup telemetry off
```

Or set `MEETING_NO_TELEMETRY=1` in your environment before starting Claude Code.

## License

MIT
