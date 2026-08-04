"""Edit Codex user configuration without disturbing unrelated settings."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path


def _split_preamble(text: str) -> tuple[str, str]:
    match = re.search(r"(?m)^\[", text)
    return (text, "") if not match else (text[: match.start()], text[match.start() :])


def _set_top_level_key(text: str, key: str, value: str) -> str:
    preamble, rest = _split_preamble(text)
    pattern = re.compile(
        rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*$"
    )
    if pattern.search(preamble):
        preamble = pattern.sub(f"{key} = {value}", preamble, count=1)
    else:
        if preamble and not preamble.endswith("\n"):
            preamble += "\n"
        preamble += f"{key} = {value}\n"
    return preamble + rest


def _remove_top_level_key(text: str, key: str) -> str:
    preamble, rest = _split_preamble(text)
    preamble = re.sub(
        rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*\r?\n?",
        "",
        preamble,
    )
    return preamble + rest


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _event_name(table_name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", table_name).lower()


def trusted_hook_hash(
    event_name: str,
    matcher: str,
    hooks: list[dict],
) -> str:
    identity = {
        "event_name": event_name,
        "hooks": [
            {
                "async": bool(hook.get("async", False)),
                "command": hook["command"],
                "timeout": int(hook.get("timeout", 600)),
                "type": hook.get("type", "command"),
            }
            for hook in hooks
        ],
        "matcher": matcher,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def remove_owned_hook_blocks(
    content: str,
    *,
    event_table: str,
    command_markers: tuple[str, ...],
) -> str:
    pattern = re.compile(
        rf"\[\[hooks\.{re.escape(event_table)}\]\][^\[]*"
        rf"(?:\[\[hooks\.{re.escape(event_table)}\.hooks\]\][^\[]*)*",
        re.DOTALL,
    )
    return pattern.sub(
        lambda match: (
            ""
            if any(marker in match.group(0) for marker in command_markers)
            else match.group(0)
        ),
        content,
    )


def rewrite_hook_state_entries(content: str, config_path: Path) -> str:
    parsed = tomllib.loads(content)
    hooks = parsed.get("hooks", {})
    event_tables = {
        name: blocks
        for name, blocks in hooks.items()
        if name != "state" and isinstance(blocks, list)
    }
    path_prefix = _toml_escape(str(config_path))
    state_pattern = re.compile(
        rf'\[hooks\.state\."{re.escape(path_prefix)}:'
        r'[a-z0-9_]+:\d+:\d+"\][^\[]*',
        re.DOTALL,
    )
    content = state_pattern.sub("", content)

    entries: list[str] = []
    for table_name, blocks in event_tables.items():
        event_name = _event_name(table_name)
        for block_index, block in enumerate(blocks):
            matcher = str(block.get("matcher") or "")
            block_hooks = block.get("hooks") or []
            block_hash = trusted_hook_hash(
                event_name,
                matcher,
                block_hooks,
            )
            for hook_index, _hook in enumerate(block_hooks):
                key = (
                    f"{config_path}:{event_name}:"
                    f"{block_index}:{hook_index}"
                )
                entries.append(
                    f'[hooks.state."{_toml_escape(key)}"]\n'
                    "enabled = true\n"
                    f'trusted_hash = "{block_hash}"\n'
                )
    if not entries:
        return content

    insert_at = len(content)
    match = re.search(r"^\[hooks\.state\.", content, re.MULTILINE)
    if match:
        insert_at = match.start()
    block_text = "\n".join(entries) + "\n"
    prefix = content[:insert_at]
    if prefix and not prefix.endswith("\n\n"):
        block_text = "\n" + block_text
    return prefix + block_text + content[insert_at:]


def remove_owned_hooks(
    config_path: Path,
    *,
    event_table: str,
    command_markers: tuple[str, ...],
) -> bool:
    if not config_path.exists():
        return False
    content = config_path.read_text(encoding="utf-8")
    updated = remove_owned_hook_blocks(
        content,
        event_table=event_table,
        command_markers=command_markers,
    )
    if updated == content:
        return False
    updated = rewrite_hook_state_entries(updated, config_path)
    config_path.write_text(updated, encoding="utf-8")
    return True


def enable_full_automation(codex_home: Path) -> None:
    config_path = codex_home / "config.toml"
    text = (
        config_path.read_text(encoding="utf-8")
        if config_path.exists()
        else ""
    )
    text = _set_top_level_key(text, "approval_policy", '"never"')
    text = _set_top_level_key(
        text,
        "sandbox_mode",
        '"danger-full-access"',
    )
    text = _remove_top_level_key(text, "default_permissions")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


def ensure_windows_unelevated_sandbox(codex_home: Path) -> None:
    config_path = codex_home / "config.toml"
    text = (
        config_path.read_text(encoding="utf-8")
        if config_path.exists()
        else ""
    )
    section = re.search(
        r"(?ms)^\[windows\][ \t]*\r?\n(.*?)(?=^\[|\Z)",
        text,
    )
    if section and re.search(
        r'(?m)^[ \t]*sandbox[ \t]*=[ \t]*"unelevated"',
        section.group(1),
    ):
        return
    if section:
        body = section.group(1)
        if re.search(r"(?m)^[ \t]*sandbox[ \t]*=", body):
            new_body = re.sub(
                r"(?m)^[ \t]*sandbox[ \t]*=.*$",
                'sandbox = "unelevated"',
                body,
                count=1,
            )
        else:
            new_body = 'sandbox = "unelevated"\n' + body
        text = text[: section.start(1)] + new_body + text[section.end(1) :]
    else:
        prefix = text.rstrip("\n") + "\n\n" if text.strip() else ""
        text = prefix + '[windows]\nsandbox = "unelevated"\n'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
