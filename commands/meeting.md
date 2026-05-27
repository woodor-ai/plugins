---
description: Manage meeting-room session — register a name, list rooms, or see candidates
argument-hint: [list | candidates | <name>]
---

# /meeting

Dispatch based on `$ARGUMENTS`:

- **Empty** (`/meeting`): Run `~/.agent-meeting/bin/room candidates`, parse it, and use the `AskUserQuestion` tool to let the user pick a name. Include ALL candidates (online / stale / historical), labeling online ones as `(in use — will conflict)`. After user picks, treat the choice as the name and proceed with registration per the meeting skill's "On `/meeting <name>`" steps.

- **`list` or `rooms`**: Run `~/.agent-meeting/bin/room list` and print the output verbatim. No registration.

- **`candidates`**: Run `~/.agent-meeting/bin/room candidates` and print the output verbatim (status, name, info).

- **Anything else (a name)**: Treat as a session name. Run the meeting skill's full registration flow:
  1. Validate name (alphanumeric + hyphen, no `--`, length 2-20)
  2. If a name is already online with a live monitor pid, warn — registering would not stop the other session's monitor, but would overwrite the directory entry. Ask explicit confirmation before proceeding.
  3. Atomic write into `~/.agent-meeting/directory.json` via `jq → tmp → mv`
  4. Run `~/.agent-meeting/bin/room init` (idempotent)
  5. Install the persistent Monitor task with the SQLite-polling script (see skills/meeting/SKILL.md for the exact zsh script — includes monitor_pid writeback for liveness)
  6. Set terminal tab title (best-effort)
  7. Confirm to user

ARGUMENTS: $ARGUMENTS
