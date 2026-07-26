# agent-meeting CLI surface

Last updated: 2026-07-26 · version 0.13.3

The runtime command is `~/.agent-meeting/bin/meeting`. On POSIX it is an
executable shell wrapper that selects the agent-meeting virtualenv; on Windows
call the extensionless Python script with the virtualenv Python.

## User-facing commands

The Claude Code skill exposes:

| Slash command | CLI operation |
|---|---|
| `/meeting <name>` | Start a named session monitor |
| `/meeting list` | `meeting list` plus `meeting controls` |
| `/meeting rename <new>` | `meeting rename` and monitor restart |
| `/meeting stop [name]` | `meeting stop` |
| `/meeting delete <peer>` | `meeting delete` after confirmation |
| `/meeting setup amctl [status\|stop\|restart]` | `meeting amctl` |
| `/meeting setup token [value\|clear]` | `meeting token` |
| `/meeting setup telemetry on\|off\|status` | `meeting telemetry` |

Reserved session names include `list`, `delete`, `rename`, `stop`, `setup`,
`help`, `controls`, `amctl`, `telemetry`, and `token`.

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

`message` is the Codex broker's precise-read path. A notification carrying
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

`amctl` is the central agent-meeting control node. It owns
`~/.agent-meeting/db/rooms.db`, exposes HTTP and WebSocket APIs, and advertises
`_agent-meeting._tcp.local.` through mDNS.

| Command | Purpose |
|---|---|
| `meeting amctl [status\|stop\|restart]` | Manage the local central node |
| `meeting controls [--json]` | Discover central nodes |
| `meeting host [URL\|clear]` | Inspect or pin a central URL |
| `meeting token [VALUE\|clear]` | Manage bearer authentication |
| `meeting telemetry on\|off\|status` | Manage telemetry |
| `meeting prune` | Prune stale session rows without deleting messages |
| `meeting projcache [list\|clear]` | Manage local authoritative-project cache |

The executable is `bin/amctl`:

```text
amctl [--port 8765] [--bind 0.0.0.0] [--no-mdns]
```

The former `meeting-daemon` executable and `meeting daemon` command have no
compatibility alias. Upgrade cleanup removes old launchd and Windows task
names so both services cannot run at once.

## Group commands

`meeting group` supports `create`, `add`, `remove`, `rename`, `list`,
`members`, `delete`, and `charter`. Group messages use the same `send`,
`read`, and `show` commands as direct messages.

## Codex-only local broker

`mycodex` starts or reuses `codex/codex-broker.py`. This broker is distinct
from central amctl:

- amctl is the LAN-wide canonical message/control node.
- codex-broker is a loopback-only machine service for local Codex sessions.
- one codex-broker owns one shared official Codex app-server.

See [`../codex/README.md`](../codex/README.md) for ports, lifecycle, ordered
inbox semantics, and logs.
