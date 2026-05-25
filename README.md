# agent-meeting

Cross-session meeting room for Claude Code agents.

Lets multiple Claude Code sessions discover each other by name, hold a persistent shared conversation, and pass turn between agents through a simple file-based protocol — no network, no daemon, just files on disk.

## Why

Each Claude Code session runs in its own process and has no built-in way to talk to another session you have open in a different terminal or worktree. `agent-meeting` gives every session a phone number (a short name), a shared directory of online peers, and a per-pair "room" file they can read/write to exchange turns.

Typical uses:

- Two sessions working on related parts of the same project coordinate without you copy-pasting between terminals.
- A "planner" session asks a "runtime" session to check something live.
- One session hands off context to another at the end of a work block.

## Install

### Marketplace (once published)

```
/plugin install agent-meeting
```

### Local path (now)

Clone, then point Claude Code at the local directory:

```bash
git clone https://github.com/Tommy-OMI/agent-meeting.git ~/code/agent-meeting
/plugin install ~/code/agent-meeting
```

The `SessionStart` hook runs on the next session and initializes `~/.claude/plugins/data/agent-meeting/`.

## Usage

### 1. Name your session

In any Claude session, first thing:

```
/meeting alice
```

Rules: lowercase letters, digits, hyphen; no `--`; length 2–20. The session writes itself into the directory, starts a background Monitor watching its inbox, and renames the terminal tab to `alice`.

### 2. Talk to a peer

Once you and at least one other named session are online:

```
/talkto bob hey, can you check whether tests pass on main?
```

Or natural-language, in any language:

- `tell bob to rerun the integration suite`
- `给 lag-runtime 打个招呼`
- `ask carol if she's still on the prep-phase branch`

The skill computes the canonical room file (sorted pair: `alice--bob.md`), writes your message into it following the room protocol, and flips the turn flag to `bob`. Bob's Monitor sees the file change within ~3 seconds and his session reads + replies.

## Protocol overview

Each pair of agents shares one canonical markdown file:

```
~/.claude/plugins/data/agent-meeting/rooms/canonical/<a>--<b>.md
```

Plus view-symlinks per side so each agent's Monitor can watch a single inbox directory:

```
~/.claude/plugins/data/agent-meeting/rooms/<self>/<peer>.md  → symlink to canonical
```

Each message block in the room looks like:

```
### [alice @ 2026-05-25 09:30] 开启
<body, ≤30 lines>

**Ask**: <one-line specific request, optional>
```

A single line near the top — `当前发言权: <name>` — is the advisory turn flag. Whoever writes flips it to the other party.

Rules enforced by skill prompts:

- Atomic full-file Write (no partial Edit, no append).
- Append-only — never modify prior messages.
- Body ≤30 lines, no long verbatim quotes, no nested tables.

## Data layout

```
~/.claude/plugins/data/agent-meeting/
├── directory.json              # online session registry
├── templates/
│   └── room-header.md          # copied here on first run, user-editable
└── rooms/
    ├── canonical/              # the real room files
    │   └── alice--bob.md
    ├── archive/                # graveyard (not auto-populated; manual move)
    ├── alice/
    │   └── bob.md → ../canonical/alice--bob.md
    └── bob/
        └── alice.md → ../canonical/alice--bob.md
```

Lock file `/tmp/meeting-dir.lock` and per-session mtime cache `/tmp/meeting-<name>.mtime` live in `/tmp` intentionally — both are ephemeral and only need to survive a session, not a reboot.

## Components

- `bin/session-bootstrap.sh` — `SessionStart` hook. Creates the data dir on first run, copies the template, then emits `additionalContext` reminding the agent it needs a name before it can call or be called.
- `skills/meeting/SKILL.md` — implements `/meeting <name>`: validate, register under `flock`, install the inbox Monitor, rename terminal tab.
- `skills/talkto/SKILL.md` — implements `/talkto <peer>` and natural-language variants: compute canonical path, create room from template, write message, flip turn.
- `templates/room-header.md` — the per-room header (protocol + turn flag), copied into the data dir on first run; user-editable thereafter.

## License

MIT — see [LICENSE](./LICENSE).
