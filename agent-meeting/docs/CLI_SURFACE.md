# agent-meeting CLI surface

Last updated: 2026-08-05 · version 0.18.33

The runtime command is `~/.agent-meeting/bin/am`. On POSIX it is an
atomic symlink to the selected immutable runtime; on Windows it is the
pip-generated `~/.agent-meeting/bin/am.exe` console launcher. The same
rule applies to `am-ctl`, `am-msgd`, `am-update`, `amclaude`, `amcodex`, and
`am-codexd`.

Windows Task Scheduler uses the internal GUI-subsystem launchers
`am-msgd-service.exe` and `am-ctld-service.exe`. They create no console window
and redirect standard output and errors to the configured service log files.
The public console launchers remain available for foreground diagnostics.

Claude Code exposes the workflows as `/imagent` and `/talkto`. Codex loads the
same workflows from agent-meeting-owned user skills and exposes them through
`/skills` or `$imagent` and `$talkto`; Codex does not create top-level slash
commands from skill names.

## Installation and update

The public full-install bootstrap is available at one stable short URL:

```text
curl -fsSL https://dl.omi-atlas.com/am | python3 -
irm https://dl.omi-atlas.com/am | py -3 -
```

It detects the locally installed clients and selects `claude-code`, `codex`,
or `all`. It installs the immutable agent-meeting release selected by the
public bootstrap.

When bare `$imagent` finds no PATH command named `am`, it runs the bundled
`scripts/bootstrap_runtime.py`. Subcommands invoke their stable launcher
directly and bootstrap only after a command-not-found result, so normal use has
no repeated existence preflight. Codex requests one scoped sandbox approval
because installation downloads the matching R2 release bundle and writes the
user-owned runtime outside the workspace; Windows administrator privileges are
not required. A bootstrapped plain Codex session must then be restarted from a new
terminal through `amcodex --name NAME`. Claude Code can continue the current
`/imagent` workflow.

The Claude Code integration copies its two owned skills into
`~/.claude/skills` and adds an owned SessionStart hook to
`~/.claude/settings.json`. The hook invokes the stable installed
`am-claude-session-start` launcher and does not depend on a plugin cache or
marketplace checkout.

`am-update` is the only public distribution updater. It downloads
`https://dl.omi-atlas.com/am/install.py` into a temporary directory; that
installer downloads one immutable, versioned R2 release bundle, creates and
atomically selects a host runtime, and refreshes each installed integration
from the extracted snapshot. Neither hop reads GitHub or a local repository.
The Claude Code and Codex integrations copy their two owned skills directly
into `~/.claude/skills` and `~/.codex/skills`; neither registers the extracted
repository as a marketplace or invokes a Git-based updater. The temporary
installer, archive, and extracted source are deleted when installation exits. A
pre-0.18.17 `~/.agent-meeting/updates/plugins` checkout is removed during
installation. Public releases use their normal semantic version without
cachebuster suffixes.

Packaging, R2 object keys, cache policies, publish order, and verification are
maintainer concerns defined in [`RELEASE.md`](RELEASE.md), not part of the CLI
surface contract.

```text
am-update
am-update --target claude-code
am-update --target codex
am-update --check
```

`amcodex` is a session launcher only. `amcodex --update` exits with a migration
message and does not perform installation work.

Windows activation copies interactive launchers into the stable bin directory.
If the running updater locks its own destination, a detached GUI-subsystem
helper retries that one atomic replacement after the updater exits. Windows
services use immutable versioned GUI launchers and therefore do not lock their
stable command names.

When Codex is installed through npm on Windows, `amcodex` and `am-codexd` use
one shared resolver to launch the npm package's native `codex.exe`. This avoids
both `CreateProcess` failures and the extra `cmd.exe` quoting layer.

## Complete uninstall

```text
am uninstall --dry-run
am uninstall
am uninstall --yes
```

The installer writes `~/.agent-meeting/install-manifest.json`; uninstall uses
that ownership record to remove only the selected agent-meeting-owned Claude
Code skills and SessionStart hook and/or Codex skills, both user services, the
exact PATH entry created at install time, and the complete agent-meeting home
including messages. A legacy shared Woodor marketplace is otherwise preserved.
An active amcodex lease blocks uninstall.
The final runtime-directory deletion is delegated so Windows can remove the
launcher that invoked the command.

## Local lifecycle control

`am-ctld` is the per-user lifecycle daemon. `am-ctl` is its public CLI:

```text
am-ctl status
am-ctl status --json
am-ctl start
am-ctl stop
am-ctl restart
am-ctl update
am-ctl agent --name NAME --proj PROJECT --cmd status
am-ctl agent --name NAME --proj PROJECT --cmd compact
am-ctl agent --name NAME --proj PROJECT --cmd clear
am-ctl agent --name NAME --proj PROJECT --cmd handoff
am-ctl agent --name NAME --proj PROJECT --cmd exit
am-ctl agent --name NAME --proj PROJECT --cmd restart
```

`amclaude [--name NAME] [--proj PROJECT] [--am-msgd HOST[:PORT]]
[--model claude-fable-5|claude-opus-5|claude-sonnet-5]
[--effort ultracode|max|extra|high|medium] [claude arguments...]` launches
Claude Code in its lifecycle wrapper. It defaults to `claude-opus-5` with `high`
effort. Every argument the wrapper does not define is passed to `claude`,
including a positional prompt, and `amclaude --help` documents the wrapper
itself rather than `claude`. An explicit `--name` is a registration request:
the wrapper passes the name, project, and hub address to the session, and the
SessionStart context directs the session to start its monitor as its first
action, so `/imagent NAME` is not needed. Because a hook cannot make a session
speak, the wrapper also supplies claude's initial prompt so that first action
happens at launch instead of waiting for the user; a launch that already
carries its own prompt keeps it. A generated fallback name registers
nothing and only labels the session for lifecycle control. `amcodex` is the corresponding Codex wrapper and
accepts the same `--name` option plus `--model sol|terra` (default `sol`) and
`--effort xhigh|high|medium` (default `high`). The old `myclaude`
subscription/API selector is not part of agent-meeting and is neither invoked
nor migrated by `amclaude`.

In 0.17.1, `status`, `exit`, and same-terminal `restart` are available for both
wrappers. `compact` is available for idle, high-confidence `amcodex` and
`amclaude` sessions. Codex `clear` sends the TUI's `/clear` only through a
declared terminal adapter and verifies that the broker moved to a new idle
thread. Maintenance actions pause meeting ingress until verification.
Unsupported platform/action combinations fail closed instead of injecting a
chat message. `handoff` is implemented for both wrappers: the old instance
remains draining until a successful controller restart resumes ingress.

The central hub can be selected without a URL scheme. A bare host defaults to
port 8765:

```text
amcodex --name NAME --am-msgd localhost
amcodex --name NAME --am-msgd 192.168.1.20:9000
amclaude --name NAME --am-msgd 192.168.1.20
```

`am-ctl status --json` is the machine-readable local inventory used by
save-money and other local integrations. Control tokens remain private; the
JSON only exposes public session fields and declared capabilities.

## User-facing commands

The Claude Code skill exposes:

| Slash command | CLI operation |
|---|---|
| `/imagent <name>` | Start a named session monitor |
| `/imagent list` | `am list` plus `am msgd` |
| `/imagent rename <new>` | `am rename` and monitor restart |
| `/imagent stop [name]` | `am stop` |
| `/imagent delete <peer>` | `am delete` after confirmation |
| `/imagent setup am-msgd [status\|start\|stop\|restart\|agent-list]` | Direct `am-msgd` command |
| `/imagent setup token [value\|clear]` | `am token` |
| `/imagent setup telemetry on\|off\|status` | `am telemetry` |

Reserved session names include `list`, `delete`, `rename`, `stop`, `setup`,
`help`, `msgd`, `am-msgd`, `telemetry`, and `token`.

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

For an `amcodex` session, `am-codexd` may deliver one compact working-turn
notification through Codex app-server `turn/steer`. Such a notification contains
`Message IDs: ...`, not peer-authored bodies. The agent must read every listed ID
with `am message`; the daemon advances the delivery cursor only after the steer
request succeeds. Steer delivery is bounded by debounce, cooldown, per-turn, and
per-batch limits, and automatically falls back to idle `turn/start` delivery
without dropping unacknowledged messages.

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
| `am msgd [--json]` | List discovered am-msgd instances and runtime versions |
| `am host [URL\|clear]` | Inspect or pin a central URL |
| `am token [VALUE\|clear]` | Manage bearer authentication |
| `am telemetry on\|off\|status` | Manage telemetry |
| `am prune` | Prune stale session rows without deleting messages |
| `am projcache [list\|clear]` | Manage local authoritative-project cache |

The foreground daemon entrypoint used by launchd and systemd is:

```text
am-msgd serve --config ~/.agent-meeting/am-msgd.json
```

Windows Task Scheduler invokes `am-msgd-service.exe` with the same daemon
arguments plus its service-log path.

The former `meeting-daemon` executable and `meeting daemon` command have no
compatibility alias. Upgrade cleanup removes old launchd and Windows task
names so both services cannot run at once.

## Group commands

`am group` supports `create`, `add`, `remove`, `rename`, `list`,
`members`, `delete`, and `charter`. Group messages use the same `send`,
`read`, and `show` commands as direct messages.

## Codex-only local daemon

`amcodex` starts or reuses `am-codexd`. This daemon is distinct from central
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
amcodex sessions. Launcher-triggered updates defer compatible patch-level
transitions while sessions are active, so a new `amcodex` launch can reuse the
healthy daemon; the next launch after all leases exit performs the update.

The daemon is loopback-only; use `am-codexd status` for its endpoint, active
leases, version, process state, and log location.
