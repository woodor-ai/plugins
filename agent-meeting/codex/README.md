# agent-meeting × codex bridge

Lets a **codex** session participate in agent-meeting — receive peer messages and
reply — the same way Claude Code sessions do. This is "form B": messages are
injected into a **live interactive `codex --remote` session** so the user sees
them (and the reply) appear on screen, and the reply is relayed back to the peer.

Parallel to `handoff/codex/`. All scripts run in place from this clone and depend
only on the `~/.agent-meeting` runtime (venv + `meeting` CLI). They honor
`MEETING_HOME` / `CODEX_HOME` for relocated / isolated installs.

## Pieces

| file | role |
|---|---|
| `install.py` | install entry point: `run_install(ctx)` interface + standalone CLI |
| `install-codex-hook.py` | writes the SessionStart register hook into `~/.codex/config.toml` |
| `codex-register.py` | the hook target — registers the session + writes the mapping file |
| `codex-bridge.py` | long-running daemon — WS `/subscribe` inbound + heartbeat, injects into the codex app-server thread, relays the reply |
| `codex-meeting.py` | launcher — one command: app-server + runtime.json + bridge + `codex --remote` + teardown |

## Install (fresh, codex-only machine)

**One-liner (PowerShell):**
```powershell
iwr -useb https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.ps1 | iex
```

**One-liner (macOS / Linux):**
```sh
curl -fsSL https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.sh | bash
```

**Manual:**
```
git clone https://github.com/woodor-ai/plugins
python <repo>/install-codex.py
```

The interactive installer copies selected plugins to `~/.codex/plugins/<name>`,
builds `~/.agent-meeting` (venv + zeroconf + websockets + `meeting` CLI), and
installs the codex SessionStart hook. On a client machine no daemon/persistence
is installed — the agent-meeting control stays on the host.

## Run a bridged live session

```
mycodex [<name>]
```

This starts the app-server + bridge in the background and drops you into a live
`codex --remote` TUI. Peers can now message `<name>`; the message appears in
your session and the reply goes back to the peer. Exit the TUI (or Ctrl-C) to
tear everything down.

## Known limitations

- **Idle detection only guards against an in-flight model turn**, not "the user is
  typing but hasn't hit enter". The bridge waits for the thread to read idle twice
  before injecting; it can still inject between a user's keystrokes.
- **No spaces in the venv-python / script paths.** Codex's Windows hook runner
  splits the command on whitespace and does not honor quotes, so the hook command
  must be two space-free tokens (the `~/.agent-meeting` venv path satisfies this).
- **`https://` / `wss://` control endpoints are not supported yet** — plaintext
  `http://` (→ plaintext ws) only.
- **Auto-warm is best-effort.** `codex-meeting` fires a minimal turn on the
  app-server right after startup (`thread/start` + `turn/start`, same protocol the
  bridge uses to inject peer messages) so the name↔session mapping exists before
  you type anything, then launches the foreground session as `codex resume
  <thread> --remote <addr>` instead of a bare `--remote`. If warm-up fails for any
  reason (app-server not ready yet, `websockets` missing, protocol error) it logs
  and falls back to a plain fresh `codex --remote <addr>` — the original
  first-turn-mapping window (see the old changelog) reopens only in that fallback
  case.
- **Control instructions (`control:restart` / `control:clear`) are injected only
  when fresh** — created within the last 10 minutes AND after this bridge process
  started. Stale or unrecognized `control:<x>` kinds are logged and dropped, never
  injected, so they can't wake the live session with noise.
- **Group charter is cached per group name for 3 minutes** inside the bridge
  process (`meeting group charter <name>`), so a burst of messages in the same
  group doesn't re-run the CLI on every single one. 1:1 messages never look up or
  inject a charter.

## Follow-ups (non-blocking)

- Hide the transient console window from background/child processes on Windows
  (`CREATE_NO_WINDOW`) — needs desktop verification. (The blank PowerShell window the
  user may see is codex's OWN command shell, a child of the codex app-server, not one
  of these scripts — it cannot be hidden from this side.)

## Note

The pre-existing **handoff** SessionStart hook exits 1 under codex on Windows
(its `python3 || py -3 || python` command form is mangled by codex's hook runner);
that is a handoff-side issue, independent of this bridge, which uses the
space-free unquoted command form and fires cleanly.
