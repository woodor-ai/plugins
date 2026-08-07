#!/usr/bin/env python3
"""Remove retired inline Codex handoff hooks without disturbing other hooks."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    tomllib = None


LEGACY_COMMAND = re.compile(r"(?:codex-)?handoff-pickup\.py")
SESSION_START_GROUP = re.compile(
    r"^\[\[hooks\.SessionStart\]\]\s*\n"
    r".*?"
    r"(?=^\[(?!\[hooks\.SessionStart\.hooks\]\])|\Z)",
    re.MULTILINE | re.DOTALL,
)
STATE_BLOCK = re.compile(
    r"^\[hooks\.state\.(?P<key>\"(?:\\.|[^\"\\])*\"|'[^']*')\]\s*\n"
    r".*?"
    r"(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
SESSION_START_STATE = re.compile(
    r"^(?P<source>.+):session_start:(?P<group>\d+):(?P<handler>\d+)$"
)
COMMAND_ASSIGNMENT = re.compile(
    r"^\s*command\s*=\s*(?P<value>\"(?:\\.|[^\"\\])*\"|'[^']*')\s*(?:#.*)?$",
    re.MULTILINE,
)


def _group_is_legacy(block: str) -> bool:
    return any(
        LEGACY_COMMAND.search(_decode_toml_string(match.group("value")))
        for match in COMMAND_ASSIGNMENT.finditer(block)
    )


def _decode_toml_string(value: str) -> str:
    if value.startswith("'"):
        return value[1:-1]
    return json.loads(value)


def migrate_config_text(content: str, config_path: Path) -> tuple[str, int]:
    groups = list(SESSION_START_GROUP.finditer(content))
    removed = {
        index
        for index, match in enumerate(groups)
        if _group_is_legacy(match.group(0))
    }
    if not removed:
        return content, 0

    source_path = str(config_path)

    def rewrite_state(match: re.Match[str]) -> str:
        key = _decode_toml_string(match.group("key"))
        parsed_key = SESSION_START_STATE.match(key)
        if parsed_key is None or parsed_key.group("source") != source_path:
            return match.group(0)

        old_index = int(parsed_key.group("group"))
        if old_index in removed:
            return ""

        new_index = old_index - sum(index < old_index for index in removed)
        if new_index == old_index:
            return match.group(0)

        new_key = (
            f"{source_path}:session_start:{new_index}:"
            f"{parsed_key.group('handler')}"
        )
        old_header, remainder = match.group(0).split("\n", 1)
        new_header = f"[hooks.state.{json.dumps(new_key, ensure_ascii=False)}]"
        return new_header + "\n" + remainder

    migrated = STATE_BLOCK.sub(rewrite_state, content)
    group_index = 0

    def remove_group(match: re.Match[str]) -> str:
        nonlocal group_index
        current = group_index
        group_index += 1
        return "" if current in removed else match.group(0)

    migrated = SESSION_START_GROUP.sub(remove_group, migrated)
    return migrated, len(removed)


def migrate_codex_config(config_path: Path) -> int:
    if not config_path.exists():
        return 0
    content = config_path.read_text(encoding="utf-8")
    migrated, removed = migrate_config_text(content, config_path)
    if not removed or migrated == content:
        return 0

    mode = stat.S_IMODE(config_path.stat().st_mode)
    temporary = config_path.with_name(f".{config_path.name}.tmp.{os.getpid()}")
    temporary.write_text(migrated, encoding="utf-8")
    temporary.chmod(mode)
    if tomllib is not None:
        tomllib.loads(temporary.read_text(encoding="utf-8"))
    os.replace(temporary, config_path)
    return removed


def main() -> None:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    migrate_codex_config(codex_home / "config.toml")


if __name__ == "__main__":
    main()
