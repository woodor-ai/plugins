# handoff update architecture

This document is the current contract for the `handoff-update` command,
distribution source, host-specific update flow, and first-time rollout.

## Command surface

```text
handoff-update
handoff-update --check
handoff-update --target claude-code
handoff-update --target codex
```

With no target, the updater detects every host where `handoff@woodor` is
actually installed and updates those hosts. A detected Claude Code or Codex
executable without an installed handoff plugin is skipped. An explicit target
fails when handoff is not installed for that host.

`--check` reports installed hosts and versions without refreshing a
marketplace or changing an installation.

## Stable command bootstrap

Starting with handoff 0.6.3, the startup SessionStart hook installs the
updater without administrator privileges:

| Platform | Stable command |
| --- | --- |
| macOS / Linux | `~/.handoff/bin/handoff-update` |
| Windows | `~/.handoff/bin/handoff-update.cmd` |

The bootstrap adds `~/.handoff/bin` to the user `PATH`. On POSIX it writes one
owned block to the active Bash, Zsh, or profile startup file. On Windows it
adds the exact directory to the user Environment `Path` value and broadcasts
the environment change. Open a new terminal after the first bootstrap.

Installation is idempotent. An unchanged launcher, updater script, or POSIX
PATH block is not rewritten on every SessionStart. After handoff itself is
updated, the next new session refreshes the stable updater from the newly
installed plugin files.

## Distribution source

handoff is a marketplace-only plugin. handoff releases do **not** produce or
upload an R2 object, immutable release bundle, or `dl.omi-atlas.com` installer.
The release commit on `main` in the following GitHub repository is the
marketplace source:

```text
https://github.com/woodor-ai/plugins.git
```

`handoff-update` does not implement its own downloader and does not read a
local Git checkout or plugin cache. It delegates marketplace refresh and
installation to the public CLI owned by each host.

### Claude Code

```text
claude plugin marketplace update woodor
claude plugin update handoff@woodor
```

The configured `woodor` marketplace is `GitHub (woodor-ai/plugins)`. Claude
Code refreshes its marketplace snapshot and installs the manifest version into
its host-managed plugin storage.

### Codex

```text
codex plugin marketplace upgrade woodor
codex plugin add handoff@woodor
```

Codex refreshes its configured Git marketplace snapshot, normally under
`~/.codex/.tmp/marketplaces/woodor`, then installs the selected version under
`~/.codex/plugins/cache/woodor/handoff/<version>`.

Both flows therefore require GitHub availability. This is intentionally
different from `am-update`, which downloads the agent-meeting installer and
immutable release bundle from R2.

## First-time rollout

Machines on handoff 0.6.2 or older do not yet contain `handoff-update`. Each
such machine needs one final host-native update to handoff 0.6.3 or newer:

```text
claude plugin marketplace update woodor
claude plugin update handoff@woodor

codex plugin marketplace upgrade woodor
codex plugin add handoff@woodor
```

Only run the pair for hosts installed on that machine. Then start one new
Claude Code or Codex session so SessionStart installs the stable command, and
open a new terminal so the PATH change is visible. All later updates use only
`handoff-update`.

## Completion and failure behavior

After updating, the command probes the installed versions again and prints the
result for each selected host. A new Codex thread or Claude Code restart is
still required because active sessions keep the skill and hook version loaded
at session start.

The updater stops on the first host CLI failure and returns a nonzero exit
status. It does not roll back a host that already completed successfully; run
`handoff-update` again after correcting the reported host CLI or marketplace
failure.
