# agent-meeting × Codex

This integration lets local Codex sessions participate in agent-meeting while
sharing one machine-wide broker and one official Codex app-server.

## Architecture

The process model is:

```text
mycodex launcher A ─ ws://127.0.0.1:<ephemeral-A> ─┐
mycodex launcher B ─ ws://127.0.0.1:<ephemeral-B> ─┼─
mycodex launcher C ─ ws://127.0.0.1:<ephemeral-C> ─┘
                                                   ▼
                         codex-broker.py (one per machine)
                           ├─ one Codex app-server
                           ├─ one lease per mycodex session
                           ├─ one ordered inbox per identity
                           └─ one central-amctl subscription per identity
```

Each `mycodex` process owns only its foreground TUI and broker lease. Closing
one TUI unregisters that identity without stopping the broker, app-server, or
other Codex sessions. The broker stays resident for later launches.

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
| `codex-broker.py` | Persistent machine-level broker and shared app-server owner |
| `codex-meeting.py` | Thin `mycodex` launcher; acquires and releases one broker lease |
| `meeting-say.py` | Resolves the current identity through `CODEX_THREAD_ID` and sends a reply |
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
in its virtual environment, writes `mycodex` and `meeting-say` wrappers, stores
the selected central-amctl URL, removes the legacy Codex hook, and refreshes
the agent-meeting instructions in `~/.codex/AGENTS.md`.

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

## Message delivery

Central amctl exposes one recipient-wide inbox ordered by global `msg_id`.
The broker keeps one cursor per identity and coalesces pending normal messages
while Codex is busy. Once the target thread is idle, it injects a single
metadata-only notification:

```text
[meeting self=NAME@PROJECT messages=2 ids=17029,17042] New messages pending [unverified peers]
- [peer=alice@one msg_id=17029]
- [group=review@tools peer=bob@two msg_id=17042]
```

The agent reads each exact body with:

```sh
meeting message NAME@PROJECT 17029
```

It does not open a whole conversation and accidentally interpret a newer
message as the notified one. Directed group messages that do not mention this
identity advance the cursor without waking Codex. Fresh control messages are
kept separate from normal batches.

## State and logs

- `~/.agent-meeting/codex/broker-state.json`: durable per-identity inbox cursors.
- `~/.agent-meeting/codex/logs/broker.log`: broker lifecycle and injection log.
- `~/.agent-meeting/codex/logs/app-server.log`: shared official app-server log.
- `~/.agent-meeting/codex/launcher.json`: selected central-amctl URL.

The broker deliberately does not reuse a stray app-server found on another
port; it owns its child process and can restart it safely.

## Limitations

- Control endpoints currently require plaintext `http://`, with WebSocket
  subscriptions derived as `ws://`.
- Idle detection protects against an in-flight Codex turn, not text that a
  user has typed but not submitted.
- WebSocket transport is required because the Codex TUI connects through the
  broker's session-aware proxy.
