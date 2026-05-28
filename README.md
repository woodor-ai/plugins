# Tommy's Claude Code Plugins

A small marketplace of Claude Code plugins for multi-session workflows, subagent orchestration, and session handoff.

## Install

```
/plugin marketplace add Tommy-OMI/plugins
/plugin install <plugin-name>
```

## Plugins

### [`agent-meeting`](./agent-meeting/)
Cross-session meeting room for Claude Code agents. SQLite-backed: atomic writes, no race conditions, unlimited history. Sessions discover each other by name and hold persistent shared conversations.

```
/plugin install agent-meeting
```

### [`init-agents`](./init-agents/)
Initialize three project-level subagent profiles (`explore` / `rd` / `planner`) tiered by model cost. Replaces ad-hoc use of built-in `Explore` / `Plan` / `general-purpose` agents with a deliberate cheap→expensive cascade.

```
/plugin install init-agents
```

### [`handoff`](./handoff/)
Session-end handoff cards with auto-pickup. Write a 50-line cue card at session end; next session's SessionStart hook auto-loads it and archives to `docs/handoff/archive/`. Zero re-typing needed to resume work.

```
/plugin install handoff
```

## License

MIT (see [LICENSE](./LICENSE)).
