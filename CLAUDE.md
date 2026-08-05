# Project instructions

Read [`docs/INDEX.md`](docs/INDEX.md) completely before doing any work in this
repository. The index defines the current documentation set and routes each
task to the documents that govern it.

This rule applies to the main agent and every worker or subagent. A worker
prompt must name `docs/INDEX.md` and the task-specific documents selected from
it; each worker must read those files itself before acting. Documents under
`docs/archive/` are historical evidence, not current instructions, unless the
index explicitly routes a task there.

Before changing a plugin version, installer, marketplace manifest, tag, or
published artifact, read [`docs/RELEASE.md`](docs/RELEASE.md) completely, then
read any plugin-specific release standard to which it routes the task.
