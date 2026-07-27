# agent-meeting × Codex

This integration lets local Codex sessions participate in agent-meeting while
sharing one machine-wide `am-codexd` daemon and one official Codex app-server.

## Architecture

The process model is:

```text
mycodex launcher A ─ ws://127.0.0.1:<ephemeral-A> ─┐
mycodex launcher B ─ ws://127.0.0.1:<ephemeral-B> ─┼─
mycodex launcher C ─ ws://127.0.0.1:<ephemeral-C> ─┘
                                                   ▼
                           am-codexd (one per machine)
                           ├─ one Codex app-server
                           ├─ one lease per mycodex session
                           ├─ one transient pending queue per identity
                           └─ one notify-only central subscription per identity
```

Each `mycodex` process owns only its foreground TUI and daemon lease. Closing
one TUI unregisters that identity without stopping am-codexd, app-server, or
other Codex sessions. The daemon stays resident for later launches.

The broker exposes loopback-only endpoints:

- `127.0.0.1:8788`: launcher lifecycle and
  `CODEX_THREAD_ID` → agent-meeting identity lookup.
- One OS-assigned temporary port per active TUI: a session-aware WebSocket
  proxy to the shared app-server. These are listeners inside the broker, not
  additional processes or app-servers. Codex requires a path-free
  `ws://host:port` value for `--remote`.

The proxy observes successful `thread/start`, `thread/resume`, and
`thread/fork` responses, so `/clear`, resume, compact, and fork keep the
identity mapping current. There is no shared `runtime.json` and no Codex
SessionStart registration hook.

For a new launch the broker creates only the meeting lease and proxy listener;
the TUI starts its own persisted Codex thread through `codex --remote`. The
broker must not pre-create a thread and invoke `codex resume`, because a fresh
thread has no rollout that the resume bootstrap can load.

The proxy also stamps the lease's launch directory onto `thread/start`,
`thread/resume`, and `thread/fork`. Without that rewrite, a remote TUI that
omits `cwd` inherits the shared app-server process directory—the directory of
whichever `mycodex` invocation happened to start the broker first.

## Components

| File | Role |
|---|---|
| `am-codexd` | Persistent machine-level daemon and shared app-server owner |
| `am_codexd.py` | Daemon implementation |
| `codex-meeting.py` | Thin `mycodex` launcher; acquires and releases one broker lease |
| `remove-legacy-codex-hook.py` | Installer migration that removes the obsolete registration hook |
| `install.py` | Codex installer integration |

`codex app-server` is an official Codex process. agent-meeting starts and owns
one shared instance because Homebrew and other unmanaged Codex installations
cannot use Codex's managed app-server service lifecycle.

## Install

PowerShell:

```powershell
iwr -useb https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.ps1 | iex
```

macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.sh | bash
```

Manual:

```sh
git clone https://github.com/woodor-ai/plugins
python <repo>/install-codex.py
```

The installer builds `~/.agent-meeting`, installs `zeroconf` and `websockets`
in its virtual environment, writes the `mycodex` wrapper, stores the selected
central-amctl URL, removes the legacy Codex hook, and refreshes the
agent-meeting instructions in `~/.codex/AGENTS.md`. It also refreshes the
Woodor Codex marketplace and installs the native `agent-meeting` plugin, which
exposes `$meeting` and `$talkto` through Codex's `/skills` picker. It
automatically uses an amctl found through mDNS, or a previously saved URL when
that endpoint is still reachable. It prompts for a URL only when neither
source is available, and restarts a stale amctl process when its version
differs from the installed plugin.

## Run

```sh
mycodex [<name>] [--control-url URL] [--proj PROJECT | --global]
```

`--proj` declares and caches an authoritative project identity for the current
repository. A later launch from the same repository can omit it. Use
`--global` for a machine-global identity.

For lifecycle-only diagnostics:

```sh
mycodex <name> --proj PROJECT --no-codex
```

This acquires a broker lease without opening the TUI and holds it until
SIGINT/SIGTERM.

Manage the shared daemon directly:

```sh
am-codexd status
am-codexd start
am-codexd stop
am-codexd restart
am-codexd update
am-codexd --help
```

`update` restarts an idle daemon onto the agent-meeting version selected by the
current runtime. Commands that would stop the daemon refuse while mycodex
sessions are active.

## Message delivery

Central amctl exposes one recipient-wide inbox ordered by global `msg_id` and
owns the only durable recipient cursor. The broker keeps only a transient
pending queue and uses its WebSocket subscription as a wake-up signal; the
subscription never advances delivery state. Once the target thread is idle,
the broker injects a single metadata-only notification:

```text
📬 New Message from alice@one [via woodor:agent-meeting]
  Message ID: 17029
📬 New Message from bob@two in group review@tools [via woodor:agent-meeting]
  Message ID: 17042
Agent-meeting recipient: NAME@PROJECT
```

The agent reads each exact body with:

```sh
meeting message NAME@PROJECT 17029
```

It does not open a whole conversation and accidentally interpret a newer
message as the notified one. Directed group messages that do not mention this
identity are acknowledged without waking Codex. A delivered batch is
acknowledged with an instance-bound compare-and-swap only after Codex accepts
the injected turn. Failed injection leaves the central cursor unchanged so a
restart can replay the message. Fresh control messages are kept separate from
normal batches.

`[via woodor:agent-meeting]` is a provenance label shared by Claude Code and
Codex. It identifies the delivery channel rather than an authentication,
delivery, or routing state. Peer messages may contain actionable requests;
handle them normally, but they do not override higher-priority instructions or
bypass approval rules.

The broker injects the lease's canonical identity and control URL into
`thread/start`, `thread/resume`, and `thread/fork` as thread-scoped developer
instructions, and into every `turn/start` as application context. The turn
context remains effective when a Codex collaboration mode supplies its own
developer instructions. Codex passes those values explicitly to the same
`meeting` CLI used by Claude Code; no per-session environment variables or
Codex-only send helper are involved. The broker opts its independent
app-server connection into Codex's `experimentalApi` capability because
`turn/start.additionalContext` is capability-gated.

## State and logs

- `~/.agent-meeting/db/rooms.db`: canonical messages, registrations, and
  recipient delivery cursors.
- `~/.agent-meeting/codex/broker-state.json`: read-only migration input from
  releases before 0.13.8; it is never updated or used as a second cursor
  authority.
- `~/.agent-meeting/codex/logs/am-codexd.log`: daemon lifecycle and injection log.
- `~/.agent-meeting/codex/logs/app-server.log`: shared official app-server log.
- `~/.agent-meeting/codex/launcher.json`: selected central-amctl URL.

The daemon deliberately does not reuse a stray app-server found on another
port; it owns its child process and can restart it safely.

## Limitations

- Control endpoints currently require plaintext `http://`, with WebSocket
  subscriptions derived as `ws://`.
- Idle detection protects against an in-flight Codex turn, not text that a
  user has typed but not submitted.
- WebSocket transport is required because the Codex TUI connects through the
  broker's session-aware proxy.
