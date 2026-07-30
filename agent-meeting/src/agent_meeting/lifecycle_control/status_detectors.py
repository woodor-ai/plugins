"""Read-only session state detectors used when no authoritative API exists."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


ZOMBIE_TIMEOUT_SECONDS = 600
CLAUDE_CONTEXT_DEFAULT = 1_000_000
CLAUDE_CONTEXT_LIMITS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
}


def _claude_project_dir(cwd: str) -> Path:
    # Claude Code's current on-disk project key replaces POSIX separators with
    # dashes. Keep this isolated so a future format change has one migration
    # point.
    key = os.path.realpath(cwd).replace("/", "-")
    return Path.home() / ".claude" / "projects" / key


def newest_claude_transcript(cwd: str) -> Path | None:
    project_dir = _claude_project_dir(cwd)
    if not project_dir.is_dir():
        return None
    candidates = list(project_dir.glob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _is_recent(timestamp: str) -> bool:
    if not timestamp:
        return True
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (
            datetime.now(timezone.utc) - value
        ).total_seconds() <= ZOMBIE_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return True


def _context_limit(model: str, overrides: dict | None) -> int:
    if overrides:
        value = overrides.get(model)
        if isinstance(value, int) and value > 0:
            return value
    return CLAUDE_CONTEXT_LIMITS.get(model, CLAUDE_CONTEXT_DEFAULT)


def detect_claude_state(
    cwd: str,
    *,
    context_limits: dict | None = None,
) -> dict:
    transcript = newest_claude_transcript(cwd)
    if transcript is None:
        return {
            "state": "unknown",
            "confidence": "low",
            "source": "transcript-missing",
        }
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {
            "state": "unknown",
            "confidence": "low",
            "source": "transcript-unreadable",
        }
    dialog = None
    last_usage = None
    last_model = ""
    last_context_event = None
    last_post_tokens = None
    compactions = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type in {"user", "assistant"}:
            dialog = event
        if event_type == "assistant":
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict) and usage:
                last_usage = usage
                last_model = str(message.get("model") or "")
                last_context_event = "assistant"
        if event_type == "system" and event.get("subtype") == "compact_boundary":
            compactions += 1
            metadata = event.get("compactMetadata")
            if isinstance(metadata, dict) and metadata.get("postTokens") is not None:
                last_post_tokens = int(metadata["postTokens"])
                last_context_event = "boundary"
    if dialog is None:
        return {
            "state": "unknown",
            "confidence": "low",
            "source": "transcript-no-dialog",
            "evidence_path": str(transcript),
        }

    recent = _is_recent(dialog.get("timestamp", ""))
    if dialog.get("type") == "user":
        state = "working" if recent else "unknown"
    else:
        message = dialog.get("message")
        stop_reason = (
            message.get("stop_reason") if isinstance(message, dict) else None
        )
        state = "working" if stop_reason == "tool_use" and recent else "idle"

    metrics = {
        "compactions": compactions,
        "transcript_session_id": transcript.stem,
    }
    window = _context_limit(last_model, context_limits)
    context_tokens = None
    if last_context_event == "boundary" and last_post_tokens is not None:
        context_tokens = last_post_tokens
    elif last_usage is not None:
        context_tokens = sum(
            int(last_usage.get(field) or 0)
            for field in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
    if context_tokens is not None:
        metrics.update(
            {
                "model": last_model or None,
                "context_tokens": context_tokens,
                "context_window": window,
                "context_utilization_pct": round(
                    context_tokens / window * 100,
                    1,
                ),
            }
        )
    return {
        "state": state,
        "confidence": "high" if recent or state == "idle" else "low",
        "source": "claude-transcript",
        "evidence_path": str(transcript),
        "evidence_timestamp": dialog.get("timestamp"),
        **metrics,
    }
