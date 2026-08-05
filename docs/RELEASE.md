# Plugin release standard

This document is the authority for repository-wide release rules and routes
each plugin to any additional plugin-specific standard. Release artifacts,
source versions, manifests, and public docs must describe the same release
before anything is published.

## Shared release rules

1. Update local `main` from the SSH remote and preserve unrelated working-tree
   changes. Release from a committed `main`, not from a temporary checkout.
2. Treat each plugin manifest version as the public cache and installation
   version. Any distributed behavior change must bump every manifest and
   package version owned by that plugin in the same commit.
3. Update user documentation, `docs/INDEX.md`, tests, and examples whenever a
   public command, supported host, install path, or dependency changes.
4. Run the target plugin's tests and all repository-level boundary tests that
   exercise its manifests or installer. A release is not complete when only
   the changed unit test passes.
5. Use English commit messages. Push and pull over SSH. Do not create a PR
   unless explicitly requested.
6. Never publish from an uncommitted tree, a moving branch archive, or a local
   marketplace/cache directory. Published inputs must be reproducible from the
   release commit.
7. Verify bytes fetched through the public URL after upload. A successful
   upload command is not delivery verification.

## Marketplace plugin releases

The repository marketplace files list plugin locations but do not duplicate
plugin versions. Marketplace releases therefore require a version bump in the
plugin manifests followed by a tested commit on `main`.

| Plugin | Hosts | Version authorities | Plugin-specific checks |
| --- | --- | --- | --- |
| `handoff` | Claude Code and Codex | both plugin manifests | card format, pickup hook, both host skill assets |
| `init-agents` | Claude Code and Codex | both plugin manifests | generated Claude/Codex profiles, conflict-safe apply behavior |
| `save-money` | Claude Code | Claude plugin manifest | every registered hook, configuration defaults, agent-meeting/handoff dependency claims |
| `init-proj` | Claude Code | Claude plugin manifest | AMBridge dependency, supported platform, generated-project claims |

For a marketplace-only plugin, push the release commit to `main` after tests.
No R2 object is produced. Keep the paired Claude/Codex manifest versions equal
for a dual-host plugin. `init-proj` remains a legacy wrapper around the private
AMBridge project-creation command; do not describe it as a standalone or
cross-platform plugin.

## Plugin-specific standards

| Plugin | Additional authority |
| --- | --- |
| `agent-meeting` | [`agent-meeting/docs/RELEASE.md`](../agent-meeting/docs/RELEASE.md) |

The agent-meeting standard owns its version authorities, minimal bundle
allowlist, R2 object keys, public install and update URLs, publish order,
delivery verification, cross-platform smoke tests, and rollback procedure.
