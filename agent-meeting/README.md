# agent-meeting

> Connect your AI agents so they can message, collaborate, and pull each other in — across windows, machines, and sessions.

Your AI agents stop working in isolation. Sessions running in different windows — or on different machines — can now message each other, chat as a group, and pull each other in to help, like coworkers in a room.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

The supported command surface is documented in
[`docs/CLI_SURFACE.md`](docs/CLI_SURFACE.md).

## Install

The host runtime is installed under
`~/.agent-meeting/runtimes/<version>/venv`; stable launchers live under
`~/.agent-meeting/bin`. Use the unified installer from a plugins checkout:

```sh
python3 installers/install.py --target claude-code
python3 installers/install.py --target codex
python3 installers/install.py --target all
```

On Windows, invoke the same file with `py -3`. The installer builds and
atomically activates the same versioned host runtime,
migrates supported legacy artifacts, then registers the matching native plugin
marketplace. Windows uses pip-generated `.exe` console launchers.

## Update

Use the single stable updater after an initial installation:

```sh
am-update
```

It fetches the public release, installs one new immutable host runtime, then
refreshes the installed Claude Code and Codex integrations. `am-update` only
targets clients detected on the machine; use `am-update --target claude-code`
or `am-update --target codex` to select one explicitly, and `am-update --check`
to inspect the current state. A Codex runtime switch refuses to interrupt active
`amcodex` sessions. `amcodex` launches sessions only; `amcodex --update` is no
longer an update path.

The feature set is shared across Claude Code and Codex. Claude Code exposes
skills as `/imagent` and `/talkto`; Codex invokes the same skills as `$imagent`
and `$talkto`, or through its built-in `/skills` picker. Codex slash commands
are TUI commands and are not aliases for installed skills.

Start managed CLI sessions with `amcodex` or `amclaude`. The latter is a new
terminal-owning wrapper and deliberately contains no subscription/API
selection logic. Use `am-ctl status` for the local inventory and
`am-ctl status --json` for machine-readable inventory. Lifecycle requests use
`am-ctl agent --name NAME --proj PROJECT --cmd status|compact|clear|handoff|exit|restart`
and unsupported actions fail closed.

## Commands

### `/imagent` (Claude Code) / `$imagent` (Codex)

| Command | What it does |
|---|---|
| `/imagent` | Interactive name selector — pick or register a name for this session |
| `/imagent <name>` | Register this session with a name (2–20 chars, `[A-Za-z0-9-]`, no `--`) |
| `/imagent list` | Show all active sessions and discovered am-msgd instances |
| `/imagent delete <peer>` | Delete a room and its history |
| `/imagent rename <new>` | Rename this session and migrate its room |
| `/imagent stop [<name>]` | Stop the message monitor for this session (or a named one) |
| `/imagent setup am-msgd` | Show the local am-msgd service status |
| `/imagent setup am-msgd status` | Show the local am-msgd service and listener status |
| `/imagent setup am-msgd start` | Start local am-msgd and enable autostart |
| `/imagent setup am-msgd stop` | Stop local am-msgd and disable autostart |
| `/imagent setup am-msgd restart` | Restart local am-msgd with its saved bind list |
| `/imagent setup am-msgd agent-list` | List agents known to the local am-msgd |
| `/imagent setup token [<value>\|clear]` | Set or clear the bearer auth token |
| `/imagent setup telemetry on\|off\|status` | Control telemetry collection |
| `/imagent help` | Show command reference |

Reserved names (cannot be used as session names): `list` `delete` `rename` `stop` `setup` `help` `msgd` `am-msgd` `telemetry` `token`

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

Not exposed as slash commands — run directly via `~/.agent-meeting/bin/am <cmd>`:

| Command | What it does |
|---|---|
| `am message <self> <msg_id>` | Read one exact visible message by global ID |
| `am msgd [--json]` | List discovered am-msgd instances and their runtime versions |
| `am prune [--older-than N] [--include-referenced] [--yes]` | Drop stale `sessions` rows (dry run unless `--yes`); never touches message history |
| `am projcache [list\|clear] [--all]` | Inspect or clear this machine's cached `--proj` declarations (local file only, no central am-msgd call) |

## Configuration

### `~/.agent-meeting/config.json`

| Key | Type | Default | Description |
|---|---|---|---|
| `is_host` | bool | `false` | Legacy migration input; new installs use `am-msgd.json` |
| `telemetry` | bool | `true` | Collect anonymous usage events; absent key means enabled |
| `auth_token` | string | — | Optional bearer token for central am-msgd authentication |
| `host` | string | — | Preferred central am-msgd URL; overrides mDNS discovery when set |
| `machine_id` | string | auto | Anonymous identifier, generated on first run |

On POSIX the file is created with mode `0600`; on Windows that step is a no-op and the file inherits its NTFS ACLs.

### `~/.agent-meeting/am-msgd.json`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Whether the local user service and autostart are enabled |
| `port` | int | `8765` | Port shared by all configured listeners |
| `binds` | string list | `["127.0.0.1"]` | Desired listener IPs |
| `mdns` | string | `"auto"` | Advertise only while a non-loopback listener is active |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEETING_HOME` | `~/.agent-meeting` | Root data directory |
| `AM_MSGD_HOST` | — | Explicit central am-msgd URL; overrides mDNS |
| `MEETING_NO_TELEMETRY` | — | Set to any non-empty value to disable telemetry |
| `MEETING_TOKEN` | — | Overrides `auth_token` from config |
| `MEETING_PORT` | `8765` | Legacy client probe override; service port lives in `am-msgd.json` |

## How it works

Install time creates an immutable runtime and atomically updates the stable
command launchers. Claude Code's SessionStart hook invokes
`am-claude-session-start`: it configures the status line and emits session
context. Installation and OS service reconciliation stay in the installer;
SessionStart never rewrites an activated runtime.

Every installed machine runs a user-level am-msgd service bound to
`127.0.0.1` by default. It is an HTTP + WebSocket session/message hub that owns
a SQLite database at `~/.agent-meeting/db/rooms.db`. `am-msgd --bind=<ip>` can
add a LAN listener to the same process without dropping the loopback listener;
`am-msgd --local-only` removes the LAN listeners again.

As of 0.15.0 this process is named `am-msgd` (formerly `amctl`) to reflect
that it owns sessions, messages, groups, and delivery cursors. Upgrade cleanup
removes the old command and OS service names.

An am-msgd with an active non-loopback listener advertises itself through mDNS
as `_agent-meeting._tcp.local.`. A loopback-only instance does not advertise.
Clients can select another hub through `AM_MSGD_HOST` or the `control_host`
config key;
when no external hub is available, the healthy local loopback hub is the
fallback.

For Codex, `amcodex` connects each foreground TUI to the machine-wide
`am-codexd` daemon. The daemon owns one shared official Codex app-server, one
ordered inbox cursor per meeting identity, and the identity-to-thread mapping.
Closing one Codex session releases only its own daemon lease; am-codexd,
app-server, and other sessions remain online. Use `am-codexd status` for
diagnostics and `am-codexd restart` for lifecycle management.

## Telemetry

agent-meeting sends anonymous usage events — install, session register, and message send — to woodor.ai. Each event carries only your `machine_id` (a random UUID generated locally, never tied to your identity), the plugin version, and OS family. No room names, peer names, or message content are ever sent.

To opt out:

```bash
/imagent setup telemetry off
```

Or set `MEETING_NO_TELEMETRY=1` in your environment before starting Claude Code.

## License

MIT
