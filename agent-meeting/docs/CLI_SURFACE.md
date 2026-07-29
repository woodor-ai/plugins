# agent-meeting CLI surface

Last updated: 2026-07-29 · version 0.16.2

The runtime command is `~/.agent-meeting/bin/am`. On POSIX it is an
atomic symlink to the selected immutable runtime; on Windows it is the
pip-generated `~/.agent-meeting/bin/am.exe` console launcher. The same
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

The central hub can be selected without a URL scheme. A bare host defaults to
port 8765:

```text
mycodex [name] --am-msgd localhost
mycodex [name] --am-msgd 192.168.1.20:9000
```

## User-facing commands

The Claude Code skill exposes:

| Slash command | CLI operation |
|---|---|
| `/imagent <name>` | Start a named session monitor |
| `/imagent list` | `am list` plus `am controls` |
| `/imagent rename <new>` | `am rename` and monitor restart |
| `/imagent stop [name]` | `am stop` |
| `/imagent delete <peer>` | `am delete` after confirmation |
| `/imagent setup am-msgd [status\|start\|stop\|restart\|agent-list]` | Direct `am-msgd` command |
| `/imagent setup token [value\|clear]` | `am token` |
| `/imagent setup telemetry on\|off\|status` | `am telemetry` |

Reserved session names include `list`, `delete`, `rename`, `stop`, `setup`,
`help`, `controls`, `am-msgd`, `telemetry`, and `token`.

## Message commands

| Command | Purpose |
|---|---|
| `am send SELF PEER [BODY]` | Insert one direct or group message |
| `am read SELF PEER` | Return conversation rows as TSV |
| `am message SELF MSG_ID` | Return exactly one visible message by global ID |
| `am show SELF PEER` | Render recent conversation history |
| `am turn SELF PEER` | Print the current direct-message turn holder |
| `am delete SELF PEER` | Delete a direct conversation |

Common options:

- `send`: `--body-file`, `--kind`, `--ask`, `--host`.
- `read`: `--limit`, `--since`, `--host`.
- `message`: `--host`.
- `show`: `--limit`, `--host`.
- `turn` and `delete`: `--host`.

`message` is am-codexd's precise-read path. A notification carrying
`msg_id=17029` must be followed by `am message SELF 17029`; opening a
whole conversation can expose later messages and lead to the wrong task being
handled.

Private `send` recipients must be written as `name@project` or `name@*`.
The CLI never auto-selects a private project from a bare name. A bare group
name remains valid when it resolves uniquely to a group.

## Session commands

| Command | Purpose |
|---|---|
| `am online NAME --cwd PATH` | Register or refresh a session |
| `am offline NAME` | Unregister a session |
| `am list` | List online, empty, and historical identities |
| `am stop NAME` | Stop a local Claude monitor |
| `am rename OLD NEW` | Rename an identity and its history |
| `am init` | Initialize the local control database |

Identity options:

- `--proj PROJECT` declares an authoritative project identity.
- `--global` uses project `*`.
- `online --instance UUID` makes reconnects from one process distinguishable
  from a different live process claiming the same identity.
- `offline --instance UUID` is intentionally hidden from normal help and lets
  a monitor or broker unregister only its own lease.
- `online --force` explicitly takes over a live identity.

## Central control

`am-msgd` is the local agent-meeting session/message hub. It owns
`~/.agent-meeting/db/rooms.db` and exposes HTTP and WebSocket APIs. It
advertises `_agent-meeting._tcp.local.` through mDNS only while an active
non-loopback listener exists.

Version 0.15.0 renamed this process and command from `amctl` to `am-msgd`
because it owns sessions and message delivery rather than merely controlling
another service. The old name is removed during upgrade and is not a second
runtime entrypoint.

| Command | Purpose |
|---|---|
| `am-msgd status [--json]` | Show service, listener, health, and connected agent address/name/project state |
| `am-msgd start\|stop\|restart` | Manage the local user service |
| `am-msgd agent-list [--json]` | List local hub identities and status |
| `am-msgd --bind IP` | Add a listener without restarting the daemon |
| `am-msgd --unbind IP` | Remove one listener without restarting the daemon |
| `am-msgd --local-only` | Remove every non-loopback listener |
| `am-msgd serve` | Internal foreground entrypoint used by OS service managers |
| `am controls [--json]` | Discover central nodes |
| `am host [URL\|clear]` | Inspect or pin a central URL |
| `am token [VALUE\|clear]` | Manage bearer authentication |
| `am telemetry on\|off\|status` | Manage telemetry |
| `am prune` | Prune stale session rows without deleting messages |
| `am projcache [list\|clear]` | Manage local authoritative-project cache |

The daemon entrypoint used by launchd, Task Scheduler, and systemd is:

```text
am-msgd serve --config ~/.agent-meeting/am-msgd.json
```

The former `meeting-daemon` executable and `meeting daemon` command have no
compatibility alias. Upgrade cleanup removes old launchd and Windows task
names so both services cannot run at once.

## Group commands

`am group` supports `create`, `add`, `remove`, `rename`, `list`,
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
mycodex sessions. Launcher-triggered updates defer compatible patch-level
transitions while sessions are active, so a new `mycodex` launch can reuse the
healthy daemon; the next launch after all leases exit performs the update.

See [`../codex/README.md`](../codex/README.md) for ports, lifecycle, ordered
inbox semantics, and logs.
