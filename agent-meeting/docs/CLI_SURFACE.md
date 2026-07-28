# agent-meeting CLI surface

Last updated: 2026-07-28 · version 0.15.3

The runtime command is `~/.agent-meeting/bin/meeting`. On POSIX it is an
atomic symlink to the selected immutable runtime; on Windows it is the
pip-generated `~/.agent-meeting/bin/meeting.exe` console launcher. The same
rule applies to `am-msgd`, `am-update`, `mycodex`, and `am-codexd`.

Claude Code exposes the workflows as `/imagent` and `/talkto`. Codex loads the
same skills from the native plugin and exposes them through `/skills` or
`$imagent` and `$talkto`; Codex does not create top-level slash commands from
skill names.

## Distribution update

`am-update` is the only public distribution updater. It refreshes the public
release checkout, creates and atomically selects one immutable host runtime,
then updates each installed integration. It does not use cachebuster version
suffixes: public releases use their normal semantic version.

```text
am-update
am-update --target claude-code
am-update --target codex
am-update --check
```

`mycodex` is a session launcher only. `mycodex --update` exits with a migration
message and does not perform installation work.

## User-facing commands

The Claude Code skill exposes:

| Slash command | CLI operation |
|---|---|
| `/imagent <name>` | Start a named session monitor |
| `/imagent list` | `meeting list` plus `meeting controls` |
| `/imagent rename <new>` | `meeting rename` and monitor restart |
| `/imagent stop [name]` | `meeting stop` |
| `/imagent delete <peer>` | `meeting delete` after confirmation |
| `/imagent setup am-msgd [status\|stop\|restart]` | `meeting am-msgd` |
| `/imagent setup token [value\|clear]` | `meeting token` |
| `/imagent setup telemetry on\|off\|status` | `meeting telemetry` |

Reserved session names include `list`, `delete`, `rename`, `stop`, `setup`,
`help`, `controls`, `am-msgd`, `telemetry`, and `token`.

## Message commands

| Command | Purpose |
|---|---|
| `meeting send SELF PEER [BODY]` | Insert one direct or group message |
| `meeting read SELF PEER` | Return conversation rows as TSV |
| `meeting message SELF MSG_ID` | Return exactly one visible message by global ID |
| `meeting show SELF PEER` | Render recent conversation history |
| `meeting turn SELF PEER` | Print the current direct-message turn holder |
| `meeting delete SELF PEER` | Delete a direct conversation |

Common options:

- `send`: `--body-file`, `--kind`, `--ask`, `--host`.
- `read`: `--limit`, `--since`, `--host`.
- `message`: `--host`.
- `show`: `--limit`, `--host`.
- `turn` and `delete`: `--host`.

`message` is am-codexd's precise-read path. A notification carrying
`msg_id=17029` must be followed by `meeting message SELF 17029`; opening a
whole conversation can expose later messages and lead to the wrong task being
handled.

Private `send` recipients must be written as `name@project` or `name@*`.
The CLI never auto-selects a private project from a bare name. A bare group
name remains valid when it resolves uniquely to a group.

## Session commands

| Command | Purpose |
|---|---|
| `meeting online NAME --cwd PATH` | Register or refresh a session |
| `meeting offline NAME` | Unregister a session |
| `meeting list` | List online, empty, and historical identities |
| `meeting stop NAME` | Stop a local Claude monitor |
| `meeting rename OLD NEW` | Rename an identity and its history |
| `meeting init` | Initialize the local control database |

Identity options:

- `--proj PROJECT` declares an authoritative project identity.
- `--global` uses project `*`.
- `online --instance UUID` makes reconnects from one process distinguishable
  from a different live process claiming the same identity.
- `offline --instance UUID` is intentionally hidden from normal help and lets
  a monitor or broker unregister only its own lease.
- `online --force` explicitly takes over a live identity.

## Central control

`am-msgd` is the central agent-meeting session/message hub. It owns
`~/.agent-meeting/db/rooms.db`, exposes HTTP and WebSocket APIs, and advertises
`_agent-meeting._tcp.local.` through mDNS.

Version 0.15.0 renamed this process and command from `amctl` to `am-msgd`
because it owns sessions and message delivery rather than merely controlling
another service. The old name is removed during upgrade and is not a second
runtime entrypoint.

| Command | Purpose |
|---|---|
| `meeting am-msgd [status\|stop\|restart]` | Manage the local central node |
| `meeting controls [--json]` | Discover central nodes |
| `meeting host [URL\|clear]` | Inspect or pin a central URL |
| `meeting token [VALUE\|clear]` | Manage bearer authentication |
| `meeting telemetry on\|off\|status` | Manage telemetry |
| `meeting prune` | Prune stale session rows without deleting messages |
| `meeting projcache [list\|clear]` | Manage local authoritative-project cache |

The executable is `bin/am-msgd`:

```text
am-msgd [--port 8765] [--bind 0.0.0.0] [--no-mdns]
```

The former `meeting-daemon` executable and `meeting daemon` command have no
compatibility alias. Upgrade cleanup removes old launchd and Windows task
names so both services cannot run at once.

## Group commands

`meeting group` supports `create`, `add`, `remove`, `rename`, `list`,
`members`, `delete`, and `charter`. Group messages use the same `send`,
`read`, and `show` commands as direct messages.

## Codex-only local daemon

`mycodex` starts or reuses `am-codexd`. This daemon is distinct from central
am-msgd:

- am-msgd is the LAN-wide canonical session/message hub.
- am-codexd is a loopback-only machine daemon for local Codex sessions.
- one am-codexd owns one shared official Codex app-server.
- am-msgd's recipient cursor is the only durable delivery position; am-codexd
  acknowledges it only after successful injection or intentional silent
  consumption.

The lifecycle command is:

```text
am-codexd status
am-codexd start
am-codexd stop
am-codexd restart
am-codexd update
am-codexd --help
```

`update` activates the agent-meeting version selected by the current runtime.
`stop`, `restart`, and a version-changing `update` refuse to interrupt active
mycodex sessions.

See [`../codex/README.md`](../codex/README.md) for ports, lifecycle, ordered
inbox semantics, and logs.
