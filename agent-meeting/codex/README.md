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
| `install.py` | codex-only install entrypoint: bootstrap runtime + install hook |
| `install-codex-hook.py` | writes the SessionStart register hook into `~/.codex/config.toml` |
| `codex-register.py` | the hook target — registers the session + writes the mapping file |
| `codex-bridge.py` | long-running daemon — WS `/subscribe` inbound + heartbeat, injects into the codex app-server thread, relays the reply |
| `codex-meeting.py` | launcher — one command: app-server + runtime.json + bridge + `codex --remote` + teardown |

## Install (fresh, codex-only machine)

```
git clone <this repo>
python <repo>/agent-meeting/codex/install.py --control-url http://<control-host>:8765
```

Builds `~/.agent-meeting` (venv + zeroconf + websockets + `meeting` CLI) and
installs the codex SessionStart hook. On a client machine no daemon/persistence
is installed — the agent-meeting control stays on the host.

## Run a bridged live session

```
<venv-python> <repo>/agent-meeting/codex/codex-meeting.py <name> --control-url http://<control-host>:8765
```

(`install.py` prints the exact command with resolved paths.) This starts the
app-server + bridge in the background and drops you into a live `codex --remote`
TUI. Peers can now message `<name>`; the message appears in your session and the
reply goes back to the peer. Exit the TUI (or Ctrl-C) to tear everything down.

## Known limitations

- **Mapping is written on the session's first turn, not at session open.** The
  register hook fires when codex runs its first turn, so a brand-new idle session
  has no `sessions/<name>.json` yet. Send one message in the codex TUI to trigger
  the first turn (this writes the mapping with the thread's `session_id`); after
  that, incoming peer messages are injected normally. Until the mapping exists the
  bridge replies with a "session not ready" notice and skips.
- **Idle detection only guards against an in-flight model turn**, not "the user is
  typing but hasn't hit enter". The bridge waits for the thread to read idle twice
  before injecting; it can still inject between a user's keystrokes.
- **No spaces in the venv-python / script paths.** Codex's Windows hook runner
  splits the command on whitespace and does not honor quotes, so the hook command
  must be two space-free tokens (the `~/.agent-meeting` venv path satisfies this).
- **`https://` / `wss://` control endpoints are not supported yet** — plaintext
  `http://` (→ plaintext ws) only.

## Follow-ups (non-blocking)

- **Single-instance protection**: nothing stops two `codex-meeting` launches for the
  same name from each starting a bridge — two bridges then both inject every message
  (double delivery), and each registers the session under its own launch-cwd project.
  Add a pidfile lock (or reuse an already-running bridge) so a second launch refuses
  or attaches instead of duplicating. (Note: on Windows one bridge shows up as two
  `python.exe` processes — the venv-python stub plus its child — which is NOT a
  duplicate; a real duplicate only comes from a second launch.)
- **Project-derivation consistency**: the launcher writes `runtime.cwd = os.getcwd()`,
  so launching from a non-project directory registers the session under the wrong
  project and inbound frames may misroute. Normalize/validate the launch cwd, or warn
  when it is not the intended project root.
- Auto-warm the session (fire an empty first turn on launch) so the mapping is
  ready immediately without the user having to send a message.
- Hide the transient console window from background/child processes on Windows
  (`CREATE_NO_WINDOW`) — needs desktop verification. (The blank PowerShell window the
  user may see is codex's OWN command shell, a child of the codex app-server, not one
  of these scripts — it cannot be hidden from this side.)

## Note

The pre-existing **handoff** SessionStart hook exits 1 under codex on Windows
(its `python3 || py -3 || python` command form is mangled by codex's hook runner);
that is a handoff-side issue, independent of this bridge, which uses the
space-free unquoted command form and fires cleanly.
