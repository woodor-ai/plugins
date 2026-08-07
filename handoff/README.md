# handoff

> Pick up exactly where you left off — no re-explaining, no lost context.

handoff leaves a short note at the end of a working session and brings it into
your next session automatically. The note keeps the current breakpoint,
pending decisions, and next actions together so work can resume immediately.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins).

## Install

Claude Code:

```text
/plugin marketplace add woodor-ai/plugins
/plugin install handoff@woodor
```

Codex:

```sh
codex plugin marketplace add woodor-ai/plugins
codex plugin add handoff@woodor
```

Start a new session after installation.

## Leave a handoff

At the end of a Claude Code session, run:

```text
/handoff
```

In Codex, run:

```text
$handoff
```

You can also ask naturally, for example: “Prepare a handoff for the next
session.”

The agent writes a short card with three parts:

1. **Current breakpoint** — unfinished work and the exact place to continue.
2. **Pending user decisions** — choices or outside events blocking progress.
3. **Next actions and leftover todos** — the first action for the new session,
   followed by the remaining tasks.

Cards stay under 30 lines so the next session receives the useful delta rather
than a long project summary.

## Resume in the next session

Open a new session in the same project. The handoff appears automatically, and
the remaining actions are added to the new session's task list. You do not need
to paste the card or explain the previous session again.

## Update

Starting with handoff 0.6.3, update every installed Claude Code and Codex copy
with one command:

```sh
handoff-update
```

Useful options:

```sh
handoff-update --check
handoff-update --target claude-code
handoff-update --target codex
```

After the first installation of handoff 0.6.3 or newer, open one new session
and then one new terminal before using `handoff-update`. After each update,
restart the Codex app or app-server, or restart Claude Code.

## Example

```markdown
# Handoff 2026-08-06 14:30 PDT

## 1. Current breakpoint
- Checkout validation is complete; the confirmation screen is not wired yet.

## 2. Pending user decisions
- Choose whether guest checkout should be enabled by default.

## 3. Next actions and leftover todos
- Run the checkout tests, then wire the confirmation screen.
- Update the release note after the tests pass.
```

## License

MIT — see [LICENSE](../LICENSE).
