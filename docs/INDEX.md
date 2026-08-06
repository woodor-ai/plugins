# Documentation index

This file is the entry point for project documentation. Every main agent,
worker, and subagent must read it before repository work, then load the
task-specific documents listed below into its own context.

## Authority and lifecycle

Current documentation lives outside `docs/archive/`. When documents disagree,
use this order:

1. repository-level `AGENTS.md` or `CLAUDE.md`;
2. this index and the current task-specific document;
3. the relevant plugin README and supported CLI surface;
4. code and tests, which must be brought back into agreement with the docs.

Completed designs, superseded contracts, investigations, and implementation
snapshots belong under `docs/archive/`. They are retained for decision history
and are never current instructions unless this index explicitly says otherwise.
When a current document becomes partly stale, update it in place. When its
entire purpose has been replaced, move it to the matching archive category and
update both this index and `docs/archive/README.md` in the same change.

## Required task routing

| Task | Documents to load |
| --- | --- |
| Repository orientation or plugin selection | `README.md` and the target plugin's `README.md` |
| Repository-wide versioning, publishing, tags, or marketplace changes | `docs/RELEASE.md` and the target plugin's `README.md` |
| agent-meeting packaging, R2 publishing, delivery verification, or rollback | `docs/RELEASE.md` and `agent-meeting/docs/RELEASE.md` |
| agent-meeting commands, installation, update, uninstall, or runtime behavior | `agent-meeting/README.md` and `agent-meeting/docs/CLI_SURFACE.md` |
| agent-meeting skill behavior or message handling | the relevant file under `agent-meeting/skills/` plus `agent-meeting/docs/CLI_SURFACE.md` |
| handoff behavior | `handoff/README.md` and `handoff/skills/handoff/SKILL.md` |
| handoff updater, distribution source, or rollout | `handoff/README.md` and `handoff/docs/UPDATING.md` |
| project-local agent profiles | `init-agents/README.md` and `init-agents/skills/init-agents/SKILL.md` |
| save-money hooks | `save-money/README.md` and the relevant hook implementation |
| legacy init-proj wrapper | `init-proj/README.md` and `init-proj/skills/init-proj/SKILL.md` |
| Historical reasoning only | `docs/archive/README.md`, then the one archive document relevant to the question |

## Current documents

| Document | Purpose |
| --- | --- |
| `README.md` | Public repository overview, installation entry points, and plugin catalog |
| `docs/RELEASE.md` | Repository-wide release rules and plugin-specific routing |
| `agent-meeting/README.md` | Public installation, update, configuration, and usage guide |
| `agent-meeting/docs/RELEASE.md` | agent-meeting packaging, R2 publishing, verification, and rollback standard |
| `agent-meeting/docs/CLI_SURFACE.md` | Supported agent-meeting command and runtime contract |
| `handoff/README.md` | Handoff user workflow, updater command, and host support |
| `handoff/docs/UPDATING.md` | Handoff updater contract, GitHub distribution source, stable command bootstrap, and rollout |
| `init-agents/README.md` | Generated worker profiles and conflict behavior |
| `init-proj/README.md` | Legacy Claude Code project-creation wrapper and its AMBridge dependency |
| `save-money/README.md` | Hook behavior, defaults, and dependencies |
| Plugin `skills/*/SKILL.md` files | Host-loaded operational instructions for each skill |

## Historical documents

The archive catalog is [`docs/archive/README.md`](archive/README.md). The
archive currently contains completed agent-meeting architecture snapshots,
superseded Codex adaptation work, closed identity contracts, and completed
design records. Runtime-created handoff cards under `docs/handoff/archive/`
are ignored project state and are not part of this documentation set.
