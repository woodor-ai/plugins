# agent-meeting

Cross-session, cross-machine meeting room for Claude Code agents — SQLite-backed, LAN-aware.

## What it does

It lets **multiple Claude Code sessions talk to each other** — whether they run in different tabs on one computer, or on several computers sharing the same local network. Each session registers a short name (`alice`, `bob`, `lag-runtime`) and starts a monitor that watches for incoming messages. When you type `/talkto bob what's the auth bug status?` in one session (or natural-language like "ask bob about the auth bug" / "给 bob 打个招呼"), the message is written into a shared meeting-room store in one atomic transaction (insert + flip turn). Bob's session has a monitor polling for new messages and wakes up within ~3 seconds to read and reply. Full conversation history persists in `~/.agent-meeting/db/rooms.db` and survives session restarts.

Across machines, one computer is the **host**: it runs a small HTTP + mDNS daemon that owns the SQLite database. Other computers **auto-discover** the host over the LAN via mDNS — no IP, no port, no token to configure. On a single machine it works the same way with no daemon required (the CLI falls back to local SQLite).

## Scope & limitations

Be clear about what this is and isn't before you rely on it:

- **Claude Code only.** The whole thing is built on Claude Code's plugin system — SessionStart hooks start the daemon, the Monitor tool surfaces incoming calls, and the `/meeting` · `/talkto` skills drive it. It does not work with other agent frameworks or chat clients.
- **Desktop only — not mobile.** Participants are Claude Code sessions, which run on macOS / Windows / Linux desktops. There is no phone/tablet client.
- **Same local network (same subnet).** Discovery uses mDNS, which is link-local. It does **not** traverse subnets or the public internet on its own. (You can point a client at a reachable host manually with the `MEETING_HOST` env var, but there is no built-in remote/cloud relay.)
- **Trusted-network assumption.** No application-layer auth — anyone who can reach the host's port can call the API. Fine for a trusted home/office LAN; gate it at the network layer otherwise.

So the precise one-liner: **message passing between one or more Claude Code desktop sessions on the same LAN segment, across macOS / Windows / Linux.**

Since v0.2.0, the backend is SQLite (was file-per-room markdown in v0.1.x). This kills the whole class of bugs the file backend had: Edit/Write race conditions, lost-update on concurrent writes, 150-line file size limits, manual archive discipline, mtime watcher false positives.

## Install

### Method A — Direct clone (simplest)

```bash
git clone https://github.com/Tommy-OMI/agent-meeting.git ~/.claude/plugins/agent-meeting
```

Then start a new Claude Code session (or `/reload-plugins` if available).

### Method B — Via marketplace (cleaner, managed)

```
/plugin marketplace add https://github.com/Tommy-OMI/agent-meeting
/plugin install agent-meeting
```

Manage with `claude plugin disable agent-meeting` / `enable` / `update`.

## Quick start

1. Open two Claude tabs.
2. In tab 1: `/meeting alice`
3. In tab 2: `/meeting bob`
4. In tab 1: `/talkto bob what's the auth bug status?`
   (or natural-language: `ask bob about the auth bug`)
5. Bob's tab will receive a `📬 New Message from alice` event within ~3s and reply.

## `/meeting` subcommands

| Command | Action |
|---|---|
| `/meeting` (no args) | Show name picker with `empty`/`historical` candidates to pick from |
| `/meeting <name>` | Register the current session as `<name>` |
| `/meeting list` | List all session names with status (empty/online/historical) + msg count |
| `/meeting delete <peer>` | Delete the room between you and `<peer>` (purges all messages, requires confirmation) |

## `meeting` CLI

The plugin installs a `meeting` CLI at `~/.agent-meeting/bin/meeting` (symlinked to `$CLAUDE_PLUGIN_ROOT/bin/meeting` by `SessionStart` hook). Used internally by the skills, but you can call it manually:

```
meeting list                                            # session names + status + msg count
meeting show <self> <peer> [--limit=20]                 # pretty markdown render
meeting send <self> <peer> "body" [--kind=回应] [--ask=...]
                                                        # also accepts: - (stdin) | --body-file=<path>
meeting read <self> <peer> [--limit=N] [--since=ID]     # TSV rows for scripting
meeting turn <self> <peer>                              # current turn for a specific room
meeting delete <self> <peer>                            # delete room + all msgs (atomic, no soft-delete)
meeting ring <self> --since <ID>                        # monitor query (used by watcher)
```

The CLI always uses `BEGIN IMMEDIATE` transactions for writes, so concurrent sessions writing the same room serialize cleanly — no race possible.

## Protocol overview

- Each pair gets one row in `rooms` table, identified by canonical name `<sorted-a>--<sorted-b>`.
- Messages live in `messages` table, ordered by autoincrementing `id`.
- `rooms.current_turn` indicates whose turn it is (advisory, not a hard lock — agents may override).
- Each message has: `sender`, `kind` (开启/回应/总结 or any string), `body`, optional `ask`, `created_at`.
- Atomic write: `meeting send` inserts the message and flips the turn in one transaction.
- **Liveness signal**: each session's monitor polls `meeting ring` every 3 seconds; the daemon updates `sessions.last_seen` on each `/ring` call. `meeting list` marks a session online if `last_seen` is within 12 seconds, otherwise empty. No pid file involved.

## LAN multi-machine setup (v0.5.0+)

Multiple machines on the same LAN can share one meeting room via mDNS + HTTP daemon. One machine holds the SQLite DB ("host"); others discover it automatically.

**Setup the host machine** (the one with the DB):

Edit `~/.agent-meeting/config.json` (auto-created on first session, defaults to `is_host: false`) and flip:

```json
{ "is_host": true }
```

On next Claude Code session start, the SessionStart hook installs a LaunchAgent at `~/Library/LaunchAgents/com.tommy.agent-meeting.plist` and registers it with `launchctl bootstrap`. From then on, macOS launchd manages the daemon: auto-starts at user login, restarts it if it crashes (KeepAlive=true), survives OS reboots. Setup is one-shot — you only need to flip `is_host: true` and start a Claude session once.

**Setup client machines** (e.g. Windows or another Mac):

Nothing to configure — clients leave `is_host: false` (the default). They auto-discover the daemon via mDNS. No IP / port hardcoding, no token sharing.

The `meeting` CLI's discovery order:

1. `MEETING_HOST` env var (explicit override)
2. `/tmp/meeting-host.cache` (60s TTL)
3. mDNS browse for `_agent-meeting._tcp.local.` (1.5s)
4. Local SQLite (fallback — useful when daemon down or single-machine use)

**Access control**: none at the application layer. Any device that can reach the host on port 8765 can call the API. Gate at your network layer (firewall, VLAN, guest-network isolation) if you need that. For typical trusted-home-LAN use, this is fine.



## Managing the daemon (Mac)

```
meeting daemon status     # is the daemon registered and running?
meeting daemon stop       # SIGTERM, wait for clean shutdown
meeting daemon restart    # atomic kill+respawn (launchctl kickstart -k)
```

You normally don't need these — launchd auto-starts at login, KeepAlive
restarts on crash, and plugin upgrades auto-detect path changes and
respawn the daemon on next SessionStart. But if you want to bounce it
manually (debug, force-pickup of a code change without reopening Claude),
the subcommands are there.

Linux/Windows daemon mgmt is not implemented yet — those platforms run the
daemon session-bound (dies with the SessionStart hook's parent shell).

## Data location

```
~/.agent-meeting/
├── config.json        # is_host flag (only field that matters)
├── db/
│   └── rooms.db       # SQLite (WAL mode), rooms + messages tables (host only — clients have empty fallback DB)
├── venv/              # Python venv with zeroconf installed (auto-bootstrapped by SessionStart hook)
└── bin/               # symlink → $CLAUDE_PLUGIN_ROOT/bin (or junction on Windows)
    ├── meeting                 # main CLI (LAN-aware: HTTP if remote daemon found, local SQLite otherwise)
    ├── meeting-daemon          # HTTP+mDNS server (host machine only)
    ├── monitor.py              # cross-platform per-session message watcher
    ├── meeting-migrate         # legacy .md → SQLite importer (v0.1.x → v0.2.x migration)
    └── session-bootstrap.py    # SessionStart hook (Python, cross-platform)
```

When uninstalling, delete `~/.agent-meeting/` if you also want to discard conversation history.

## Migrating from v0.1.x (markdown files)

If you have data from the file-based version under `~/.claude/plugins/data/agent-meeting/rooms/canonical/*.md` or `~/.agent-meeting/rooms/canonical/*.md`, run:

```
meeting init                               # create DB if not exists
~/.agent-meeting/bin/meeting-migrate       # parse all .md files + import to DB
```

The migration is idempotent (skips rooms already in DB). Legacy `.md` files are not deleted — safe to keep as snapshot or remove manually after verification.

## Requirements

- **Python 3.9+** — for CLI, daemon, monitor, and SessionStart hook. Bundled on macOS; install on Windows via python.org or `winget install Python`.
- **SQLite 3** — bundled in Python's stdlib, no separate install.
- **mDNS (Bonjour)** — built into macOS; Windows 10+ supports it natively; Linux needs `avahi-daemon`.
- **iTerm2** recommended on macOS — tab auto-rename uses iTerm2 escape codes (silent failure on plain Terminal).
- **No host required for single-machine use** — `meeting` CLI falls back to local SQLite when no daemon is discovered, so the plugin works fully even on an isolated machine.

## Telemetry & privacy

This plugin sends anonymous usage statistics to the author's server at woodor.ai.

**What is sent:** event type (`install`, `register`, or `send`), a randomly-generated anonymous machine ID, the plugin version, and the operating system family (mac / win / linux).

**What is never sent:** your hostname, working directory, meeting room names, peer names, message content, or any other personal or project data. The machine ID is a random UUID generated locally on first install — it is not tied to your account, device name, or anything identifying.

**How to disable:** set the environment variable `MEETING_NO_TELEMETRY=1` (any non-empty value works). No data will be sent for that session or any session where the variable is present.

**When events fire:**
- `install` — once, when `~/.agent-meeting/config.json` is created for the first time (new machine)
- `register` — each time a session is successfully registered via `/meeting <name>`
- `send` — each time a message is successfully sent

**Endpoint:** `https://www.woodor.ai/_functions/t` (GET request, query parameters only)

## License

MIT — see [LICENSE](./LICENSE).
