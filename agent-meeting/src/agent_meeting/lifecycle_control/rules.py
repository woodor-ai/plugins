"""Configuration-driven, fail-closed lifecycle rule evaluation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuleDecision:
    command: str
    reason: str


DEFAULTS = {
    "enabled": False,
    "action_cooldown_seconds": 600,
    "max_consecutive_failures": 3,
    "compact_token_pct": 60.0,
    "handoff_token_pct": 80.0,
    "max_compactions": 2,
}


def _number(value, default, converter, *, minimum=None):
    try:
        result = converter(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def load_rule_config(path: Path) -> dict:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        payload = {}
    automation = payload.get("automation") or {}
    if not isinstance(automation, dict):
        automation = {}
    codex = payload.get("codex") or {}
    claude = payload.get("claude") or {}

    def platform_config(section: dict) -> dict:
        if not isinstance(section, dict):
            section = {}
        context_limits = section.get("context_limits") or {}
        if not isinstance(context_limits, dict):
            context_limits = {}
        return {
            "compact_token_pct": _number(
                section.get(
                    "compact_token_pct",
                    DEFAULTS["compact_token_pct"],
                ),
                DEFAULTS["compact_token_pct"],
                float,
                minimum=0,
            ),
            "handoff_token_pct": _number(
                section.get(
                    "handoff_token_pct",
                    DEFAULTS["handoff_token_pct"],
                ),
                DEFAULTS["handoff_token_pct"],
                float,
                minimum=0,
            ),
            "max_compactions": _number(
                section.get(
                    "max_compactions",
                    DEFAULTS["max_compactions"],
                ),
                DEFAULTS["max_compactions"],
                int,
                minimum=0,
            ),
            "context_limits": {
                str(key): int(value)
                for key, value in context_limits.items()
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            },
        }

    codex_config = platform_config(codex)
    claude_config = platform_config(claude)
    return {
        **DEFAULTS,
        "enabled": automation.get("enabled") is True,
        "action_cooldown_seconds": _number(
            automation.get(
                "action_cooldown_seconds",
                DEFAULTS["action_cooldown_seconds"],
            ),
            DEFAULTS["action_cooldown_seconds"],
            int,
            minimum=0,
        ),
        "max_consecutive_failures": _number(
            automation.get(
                "max_consecutive_failures",
                DEFAULTS["max_consecutive_failures"],
            ),
            DEFAULTS["max_consecutive_failures"],
            int,
            minimum=1,
        ),
        "codex": codex_config,
        "claude": claude_config,
        # Keep the 0.17.0 pre-release flat keys readable for callers that only
        # understood Codex settings.
        **{
            key: codex_config[key]
            for key in (
                "compact_token_pct",
                "handoff_token_pct",
                "max_compactions",
            )
        },
    }


def evaluate_session(session: dict, config: dict) -> RuleDecision | None:
    if not config.get("enabled"):
        return None
    wrapper = session.get("wrapper")
    if wrapper not in {"amcodex", "amclaude"}:
        return None
    if session.get("state") != "idle" or session.get("confidence") != "high":
        return None
    platform = "codex" if wrapper == "amcodex" else "claude"
    platform_config = config.get(platform) or config
    utilization = session.get("context_utilization_pct")
    compactions = int(session.get("compactions") or 0)
    if compactions >= int(platform_config["max_compactions"]):
        return RuleDecision(
            "handoff",
            f"compactions={compactions} reached configured maximum",
        )
    if utilization is None:
        return None
    if float(utilization) >= float(platform_config["handoff_token_pct"]):
        return RuleDecision(
            "handoff",
            f"context utilization {utilization}% reached handoff threshold",
        )
    if float(utilization) >= float(platform_config["compact_token_pct"]):
        return RuleDecision(
            "compact",
            f"context utilization {utilization}% reached compact threshold",
        )
    return None
