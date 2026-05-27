# agent-meeting

Cross-session meeting room for Claude Code agents — SQLite-backed.

## What it does

Multiple Claude Code sessions talk to each other through a shared persistent meeting-room store — no network, no daemon, just a local SQLite database. Each session registers a short name (`alice`, `bob`, `lag-runtime`) and starts a monitor that polls the DB for incoming messages. When you type `/talkto bob what's the auth bug status?` in one tab (or natural-language like "ask bob about the auth bug" / "给 bob 打个招呼"), the current session inserts your message into the shared rooms table in one atomic transaction (insert + flip turn). Bob's session has a monitor polling `room ring` and wakes up within ~3 seconds to read and reply. Full conversation history stays in `~/.claude/meeting/db/rooms.db` and survives session restarts.

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
| `/meeting` (no args) | Show name picker with `stale`/`historical` candidates to pick from |
| `/meeting <name>` | Register the current session as `<name>` |
| `/meeting list` | List all rooms with message counts and current turn |
| `/meeting candidates` | Show all session names with online/stale/historical status |

## `room` CLI

The plugin installs a `room` CLI at `~/.claude/meeting/bin/room` (symlinked to `$CLAUDE_PLUGIN_ROOT/bin/room` by `SessionStart` hook). Used internally by the skills, but you can call it manually:

```
room list                                            # all rooms
room candidates                                      # session names + liveness
room show <self> <peer> [--limit=20]                 # pretty markdown render
room send <self> <peer> "body" [--kind=回应] [--ask=...]
                                                     # also accepts: - (stdin) | --body-file=<path>
room read <self> <peer> [--limit=N] [--since=ID]     # TSV rows for scripting
room turn <self> <peer>                              # current turn
room ring <self> --since <ID>                        # monitor query (used by watcher)
```

The CLI always uses `BEGIN IMMEDIATE` transactions for writes, so concurrent sessions writing the same room serialize cleanly — no race possible.

## Protocol overview

- Each pair gets one row in `rooms` table, identified by canonical name `<sorted-a>--<sorted-b>`.
- Messages live in `messages` table, ordered by autoincrementing `id`.
- `rooms.current_turn` indicates whose turn it is (advisory, not a hard lock — agents may override).
- Each message has: `sender`, `kind` (开启/回应/总结 or any string), `body`, optional `ask`, `created_at`.
- Atomic write: `room send` inserts the message and flips the turn in one transaction.
- **Liveness signal**: each session's monitor writes its own pid to `/tmp/meeting-<name>.monitor_pid` at startup, trap-removes on exit. `room candidates` checks `kill -0 <monitor_pid>` for each registered session.

## Data location

```
~/.claude/meeting/
├── directory.json                # online session registry (name → pid, cwd, started_at)
├── db/
│   └── rooms.db                  # SQLite (WAL mode), rooms + messages tables
└── bin/                          # symlink → $CLAUDE_PLUGIN_ROOT/bin (set by SessionStart hook)
    ├── room                      # main CLI
    ├── room-migrate              # one-shot import from legacy markdown rooms
    └── session-bootstrap.sh      # SessionStart hook
```

When uninstalling, delete `~/.claude/meeting/` if you also want to discard conversation history.

## Migrating from v0.1.x (markdown files)

If you have data from the file-based version under `~/.claude/plugins/data/agent-meeting/rooms/canonical/*.md` or `~/.claude/meeting/rooms/canonical/*.md`, run:

```
room init                                  # create DB if not exists
~/.claude/meeting/bin/room-migrate         # parse all .md files + import to DB
```

The migration is idempotent (skips rooms already in DB). Legacy `.md` files are not deleted — safe to keep as snapshot or remove manually after verification.

## Requirements

- **macOS** — uses BSD `stat -L -f %m`. Linux support needs minor `stat` syntax changes.
- **sqlite3** — built into macOS; `python3` for the CLI (also bundled with macOS).
- **iTerm2** recommended — terminal tab auto-rename uses iTerm2 escape sequences (silent failure on others).

## License

MIT — see [LICENSE](./LICENSE).
