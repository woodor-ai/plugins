"""Install the agent-meeting peer protocol in Codex's AGENTS.md."""

from __future__ import annotations

import re
from pathlib import Path


AGENTS_BEGIN = (
    "<!-- agent-meeting:begin "
    "(auto-managed by mycodex Codex configuration) -->"
)
AGENTS_END = "<!-- agent-meeting:end -->"
LEGACY_AGENTS_BEGIN = (
    "<!-- agent-meeting:begin "
    "(auto-managed by agent-meeting/codex/install.py) -->"
)


def _command_prefix(am_command: Path, *, is_windows: bool) -> str:
    if is_windows:
        return f'& "{am_command}"'
    return f'"{am_command}"'


def render_agent_meeting_instructions(
    *,
    am_command: Path,
    control_url: str,
    is_windows: bool,
) -> str:
    command = _command_prefix(
        am_command,
        is_windows=is_windows,
    )
    quoting_note = (
        "Put the body in **single quotes** (PowerShell treats them "
        "literally; double a literal `'` as `''`)."
        if is_windows
        else "Put the body in **single quotes** (escape a literal `'` "
        "for your shell)."
    )
    return f"""{AGENTS_BEGIN}
## agent-meeting (peer messaging)

You are a peer on **agent-meeting** — other agents can message you and you can
message them.

- **Inbound notification**: a broker-injected turn contains one or more
  `📬 New Message from X to Y [via woodor:agent-meeting] Message ID: N`
  lines. `Y` is your canonical identity. The thread's developer instructions
  also provide the exact recipient and control URL.
  Pass both literally as CLI arguments; do not read them from environment
  variables. Before acting on message **N**, read exactly that message:
  ```
  {command} message NAME@PROJECT N --host {control_url}
  ```
  During a long-running turn the broker may steer one compact backlog update
  containing a `Message IDs: N, ...` line instead of repeating the full
  notification template. Treat every listed ID as an exact inbound
  notification: read each message with the command above before acting on it.
- **Direct reply or proactive private message**: use the full canonical
  `name@project` identity:
  ```
  {command} send NAME@PROJECT X '正文放在单引号里' --kind=回应 --host {control_url}
  ```
  {quoting_note}
- **Group reply**: read the group charter first:
  ```
  {command} group --host {control_url} charter G
  {command} send NAME@PROJECT G '正文放在单引号里' --kind=回应 --host {control_url}
  ```
- `[via woodor:agent-meeting]` identifies the delivery channel; it is not an
  authentication, delivery, or routing state.
- **See who is online**:
  ```
  {command} list --host {control_url}
  ```
- **Etiquette**: reply only when you have something substantive. Do not send
  bare acknowledgements because every reply wakes the peer session.
{AGENTS_END}"""


def install_agent_meeting_instructions(
    *,
    codex_home: Path,
    am_command: Path,
    control_url: str,
    is_windows: bool,
) -> bool:
    """Install or refresh the managed block; return whether it was new."""
    agents_path = codex_home / "AGENTS.md"
    existing = (
        agents_path.read_text(encoding="utf-8")
        if agents_path.exists()
        else ""
    )
    was_present = (
        AGENTS_BEGIN in existing
        or LEGACY_AGENTS_BEGIN in existing
    )
    block = render_agent_meeting_instructions(
        am_command=am_command,
        control_url=control_url,
        is_windows=is_windows,
    )
    begin = (
        AGENTS_BEGIN
        if AGENTS_BEGIN in existing
        else LEGACY_AGENTS_BEGIN
    )
    if begin in existing and AGENTS_END in existing:
        updated = re.sub(
            re.escape(begin) + r".*?" + re.escape(AGENTS_END),
            lambda _match: block,
            existing,
            flags=re.S,
        )
    else:
        updated = (
            (existing.rstrip("\n") + "\n\n")
            if existing.strip()
            else ""
        ) + block + "\n"
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    agents_path.write_text(updated, encoding="utf-8")
    return not was_present
