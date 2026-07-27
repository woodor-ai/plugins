# Plugins Codex Architecture Audit

## Executive conclusion

The current layout is structurally confusing, not merely poorly named.

Codex installation, runtime selection, command generation, platform adaptation,
and SessionStart repair are owned by several overlapping systems. The result is
multiple installed copies at different versions and no obvious answer to
"which file is running?"

The architectural direction should be:

1. Make `codex/` a top-level distribution layer for the whole plugins
   repository.
2. Move `mycodex` and the local Codex daemon out of `agent-meeting`.
3. Make the Codex native plugin cache the only plugin installation authority.
4. Package the agent-meeting runtime instead of copying individual scripts into
   `~/.agent-meeting/bin`.
5. Split SessionStart context emission, runtime installation, and platform
   service management.
6. Keep only two user-facing commands: `meeting` and `mycodex`.
7. Remove compatibility cleanup code after a one-time controlled migration.

## Current sources and installed copies

The development machine currently has five agent-meeting trees:

| Location | Observed version | Role |
|---|---:|---|
| `/Users/tommyclaw/AIAgent/plugins/agent-meeting` | 0.14.0 | Development source |
| `~/.codex/plugins-src/agent-meeting` | 0.13.6 | Persistent updater checkout |
| `~/.codex/plugins/agent-meeting` | 0.13.8 | Legacy installer copy |
| `~/.codex/plugins/cache/woodor/agent-meeting/0.14.0` | 0.14.0 | Native Codex plugin cache |
| `~/.agent-meeting/bin` | Generated mixture | Runtime wrappers and copied scripts |

The active runtime currently points at the native 0.14.0 cache through
`~/.agent-meeting/.bin-plugin-root`. Older trees remain present and look
equally authoritative to a developer inspecting the filesystem.

## Current installation chain

```text
install-codex-plugins.sh / install-codex-plugins.ps1
  -> clone or pull ~/.codex/plugins-src
  -> run install-codex.py
  -> copy selected plugins into ~/.codex/plugins/<name>
  -> run agent-meeting/codex/install.py from that copy
  -> run agent-meeting/bin/session-bootstrap.py
  -> generate ~/.agent-meeting/bin
  -> install the same plugin again through `codex plugin add`
  -> create a versioned native plugin cache
  -> next SessionStart runs bootstrap from the native cache
  -> rewrite ~/.agent-meeting/bin to point at the native cache
```

The same install invokes both the legacy copy-based plugin model and the native
Codex plugin model.

## Current update and launch chains

`mycodex --update` repeats repository checkout logic already present in the two
remote bootstrap scripts, then reruns the root installer.

`am-codexd update` does something different: it only restarts the local daemon
onto the version selected by the current runtime. It does not update plugin
source.

The launch chain is:

```text
~/.agent-meeting/bin/mycodex
  -> read ~/.agent-meeting/.bin-plugin-root
  -> locate the selected agent-meeting plugin tree
  -> codex/codex-meeting.py
  -> bin/am-codexd
  -> codex/am_codexd.py
  -> shared Codex app-server
  -> Codex TUI through a per-session proxy
```

The launcher crosses the runtime directory, a pointer file, the native plugin
cache, two source directories, and two daemon layers.

## Root causes

### Multiple installation authorities

The root installer, the plugin-specific Codex installer, and the SessionStart
bootstrap all write or regenerate runtime state.

### Legacy and native installation models coexist

`install-codex.py` copies a complete plugin into
`~/.codex/plugins/<name>`, then installs the same plugin through the Codex
native marketplace. Both trees survive upgrades.

### SessionStart is a general-purpose installer

`session-bootstrap.py` is approximately 1,275 lines and currently owns:

- directory and virtual environment creation;
- dependency installation;
- command-wrapper generation;
- runtime version selection and downgrade prevention;
- macOS launchd management;
- Windows Startup, scheduled-task, and supervisor management;
- Linux control startup;
- Claude Code status-line configuration;
- SessionStart context output;
- telemetry;
- legacy artifact cleanup.

The plugin installer calls this SessionStart hook and then parses its
Claude-facing JSON to produce installer output. That is a direct responsibility
violation.

### `mycodex` is owned by the component it bootstraps

`mycodex` updates the Woodor Codex plugin suite and launches Codex, but its
source is stored inside `agent-meeting/codex` and agent-meeting is responsible
for repairing its installed copy.

The dependency direction is inverted: the top-level launcher is owned by a
lower-level messaging plugin.

### Directory names do not express one taxonomy

The current `bin/` and `codex/` split mixes several classification rules:

- public executable versus implementation;
- generic runtime versus Codex-specific runtime;
- platform wrapper versus cross-platform Python;
- installer versus runtime;
- Claude Code integration versus machine service.

For example, `bin/am-codexd` is Codex-specific, while its implementation lives
at `codex/am_codexd.py`.

### Platform behavior is scattered

POSIX and Windows handling appears in:

- two root bootstrap scripts;
- three `mycodex` wrapper files;
- the root installer;
- the plugin installer;
- SessionStart bootstrap;
- the meeting CLI;
- the Windows supervisor.

### Permanent migration code

Runtime paths still contain cleanup for obsolete names such as
`codex-plugins`, `meeting-say`, `meeting-daemon`, and old Codex hooks. Internal
tools should perform one explicit migration and then delete the compatibility
logic.

## Target ownership model

The repository root should represent a plugin marketplace plus one Codex
distribution layer:

```text
plugins/
├── README.md
├── LICENSE
├── .claude-plugin/
│
├── codex/
│   ├── install.sh
│   ├── install.ps1
│   ├── install.py
│   ├── README.md
│   └── mycodex/
│       ├── launcher.py
│       ├── daemon.py
│       ├── daemon_cli.py
│       └── platform/
│           ├── posix.py
│           └── windows.py
│
├── agent-meeting/
│   ├── .claude-plugin/
│   ├── .codex-plugin/
│   ├── pyproject.toml
│   ├── skills/
│   ├── hooks/
│   │   ├── hooks.json
│   │   └── session_start.py
│   ├── installer/
│   │   └── runtime.py
│   ├── src/agent_meeting/
│   │   ├── cli.py
│   │   ├── control_server.py
│   │   ├── monitor.py
│   │   ├── discovery.py
│   │   ├── config.py
│   │   ├── claude/
│   │   │   └── statusline.py
│   │   └── services/
│   │       ├── macos.py
│   │       ├── windows.py
│   │       └── linux.py
│   ├── migrations/
│   └── tests/
│
├── handoff/
├── init-agents/
└── save-money/
```

## Responsibility boundaries

### Top-level `codex/`

Owns:

- initial Codex installation;
- installation and update of Woodor Codex plugins;
- the `mycodex` command;
- the local Codex app-server daemon;
- Codex-specific platform launching.

It may depend on agent-meeting, but agent-meeting must not generate or repair
`mycodex`.

### `agent-meeting`

Owns:

- peer identity and messaging;
- the `meeting` CLI;
- the central control server;
- discovery and transport;
- Claude Code monitor and status-line integration;
- control-node persistence on macOS, Windows, and Linux.

It does not own Codex installation, the Codex app-server, or the top-level
launcher.

### SessionStart hook

Owns only:

- checking that the runtime is available;
- emitting session context;
- returning a non-blocking result on failure.

It must not contain dependency installation, platform service implementations,
wrapper generation, or migration cleanup.

### Runtime installer

Owns only:

- creating or upgrading the agent-meeting runtime environment;
- installing the packaged agent-meeting code;
- installing platform service definitions;
- recording one authoritative runtime version.

## Public command surface

Only two commands should be installed on the user PATH:

```text
meeting
mycodex
```

Suggested user-facing operations:

```text
meeting control start|status|stop|restart
mycodex [session options]
mycodex update
mycodex doctor
mycodex debug daemon status|restart
```

`amctl` and `am-codexd` should be internal process modules rather than separate
top-level user commands.

## Target installation model

```text
remote platform bootstrap
  -> download a temporary repository archive
  -> run codex/install.py
  -> install plugins through the native Codex plugin manager
  -> install the independent mycodex runtime
  -> remove the temporary archive
```

Consequences:

- no persistent `~/.codex/plugins-src`;
- no legacy `~/.codex/plugins/<name>` copies;
- native Codex plugin cache is the sole plugin installation authority;
- no `.bin-plugin-root`;
- no individual Python-file copying into `~/.agent-meeting/bin`;
- no hand-built POSIX/Windows wrappers when Python package entry points can
  generate them;
- one writer for each runtime;
- one recorded active version for each runtime.

Suggested runtime ownership:

```text
~/.codex/woodor/        # mycodex runtime and state
~/.agent-meeting/       # agent-meeting runtime, data, logs, and service state
```

`mycodex` should not be installed under `~/.agent-meeting/bin`.

## Current-to-target file mapping

| Current file or area | Target |
|---|---|
| Root `install-codex-plugins.sh` | `codex/install.sh` |
| Root `install-codex-plugins.ps1` | `codex/install.ps1` |
| Root `install-codex.py` | `codex/install.py` |
| `agent-meeting/codex/mycodex-*` | Top-level `codex/mycodex/platform/` or package entry points |
| `agent-meeting/codex/codex-meeting.py` | `codex/mycodex/launcher.py` |
| `agent-meeting/codex/am_codexd.py` | `codex/mycodex/daemon.py` |
| `agent-meeting/bin/am-codexd` | Remove as a public command |
| `agent-meeting/bin/meeting` | `agent-meeting/src/agent_meeting/cli.py` |
| `agent-meeting/bin/amctl` | `agent-meeting/src/agent_meeting/control_server.py` |
| `agent-meeting/bin/meeting_common.py` | Split into named package modules |
| `agent-meeting/bin/monitor.py` | Agent-meeting runtime monitor module |
| `agent-meeting/bin/statusline.py` | Claude Code integration module |
| `agent-meeting/bin/supervisor.py` | Windows service module |
| `agent-meeting/bin/session-bootstrap.py` | Split across hook, runtime installer, and service modules |
| `remove-legacy-codex-hook.py` | Run once during cutover, then delete |

## Migration strategy

This should be implemented as one breaking architectural release. Development
may use multiple commits, but no compatibility period should be published.

1. Create the top-level Codex distribution package.
2. Move `mycodex`, launcher, and daemon ownership out of agent-meeting.
3. Convert agent-meeting into a normal Python package with generated command
   entry points.
4. Split the SessionStart hook from runtime installation and service
   management.
5. Replace copy-based plugin installation with native Codex plugin
   installation.
6. Remove persistent updater checkout and runtime pointer indirection.
7. Delete legacy installers, wrapper generators, and permanent cleanup paths.
8. Perform one explicit cleanup of old Mac and Windows install directories and
   service definitions.
9. Verify:
   - fresh POSIX install;
   - fresh Windows install;
   - upgrade from the current release;
   - Claude Code registration and messaging;
   - Codex launch and message injection;
   - cross-machine control discovery;
   - clean daemon shutdown and restart.

## Final architectural decision

`mycodex` is not an agent-meeting subcomponent and is not a generic tool under
`~/AIAgent/tools`.

It is the Codex distribution and launch layer for this plugins repository, so
its source belongs under the repository's top-level `codex/` directory.

Agent-meeting remains a plugin and runtime service consumed by that layer.
