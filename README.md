# agent-meeting

Cross-session meeting room for Claude Code agents.

## What it does

Multiple Claude Code sessions talk to each other through a shared file-based meeting-room protocol — no network, no daemon, just files on disk. Each session registers a short name (`alice`, `bob`, `lag-runtime`) and gets an inbox watcher. When you type `/talkto bob what's the auth bug status?` in one tab (or natural-language like "ask bob about the auth bug" / "给 bob 打个招呼"), the current session writes your message into the canonical room file shared by both sides. Bob's session has a Monitor watching that file's mtime and wakes up within ~3 seconds to read and reply. The room file is append-only markdown, so the full conversation history stays on disk and survives session restarts.

## Install

### Method A — Direct clone (simplest)

```bash
git clone https://github.com/Tommy-OMI/agent-meeting.git ~/.claude/plugins/agent-meeting
```

Then in any open Claude session, run `/reload-plugins`, or just start a new session.

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
5. Bob's tab will auto-RING within ~3s and reply.

## Protocol overview

- One room file per pair, named `<sorted-a>--<sorted-b>.md`, stored under `~/.claude/plugins/data/agent-meeting/rooms/canonical/`.
- A header line `当前发言权: <name>` indicates whose turn it is (advisory, not a hard lock).
- Each message block:

  ```
  ### [<name> @ <YYYY-MM-DD HH:MM>] <开启|回应|总结>
  <body>

  **Ask**: <optional one-line specific request>
  ```

- Atomic write: agents Read-then-Write the entire file in one Write call.
- Append-only **within active topic**; on topic close, agents archive the closed thread to `<data-root>/archive/` and replace it in the main room file with a single Topic Index row pointing at the archive. Keeps long-running rooms bounded (~150 line cap on the main file).

## Data location

All runtime state lives under:

```
~/.claude/plugins/data/agent-meeting/
├── directory.json     # online session registry
├── templates/         # user-editable room header template
├── rooms/             # canonical room files + per-side inbox symlinks (active topic only)
└── archive/           # closed-topic threads moved out of main room files
```

When uninstalling the plugin, manually delete this directory if you also want to discard the conversation history (active rooms + archived topics).

## Requirements

- **macOS** — uses BSD `stat -L -f %m` syntax in the Monitor watcher. Linux support requires changing `stat` invocations in `bin/` and `skills/`.
- **iTerm2** recommended — terminal tab auto-rename uses iTerm2 escape sequences.

## License

MIT — see [LICENSE](./LICENSE).
