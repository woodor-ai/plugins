"""Edit Codex config.toml without disturbing unrelated user settings."""

from __future__ import annotations

import re
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
