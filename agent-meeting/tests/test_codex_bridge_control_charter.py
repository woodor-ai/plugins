"""
Regression tests for codex-bridge.py: control:* instructions + group charter
injection (agent-meeting v0.8.40).

Tests are function-level inline replicas (same convention as
test_bridge_logic.py) — codex-bridge.py parses argv at import time, so it is
not directly importable; the replicas mirror the real functions closely
enough that a change to the real logic without a matching test update is
easy to spot in review.
"""

import time

import pytest


# ---------------------------------------------------------------------------
# control:* freshness + routing — inline replica of codex-bridge._handle_control
# ---------------------------------------------------------------------------

_CONTROL_STALE_S = 600

_CONTROL_DIRECTIVES = {
    "restart": "Write a handoff card summarizing in-flight state now, then stop "
               "accepting new tasks and wait for this session to end.",
    "clear": "Abort whatever task is in flight, clear your working context, and "
             "report back that you have been cleared.",
}


def _handle_control(room, mid, created, sender, kind, now, bridge_start):
    action = kind.split(":", 1)[1] if ":" in kind else ""
    age_s = now - created
    if age_s > _CONTROL_STALE_S or created < bridge_start:
        return None
    directive = _CONTROL_DIRECTIVES.get(action)
    if directive is None:
        return None
    return f"[control:{action} from peer={sender}] {directive}"


def test_control_restart_fresh_is_injected():
    now = 1_000_000
    bridge_start = now - 100
    text = _handle_control("alice", 1, now - 5, "alice", "control:restart", now, bridge_start)
    assert text == (
        "[control:restart from peer=alice] Write a handoff card summarizing "
        "in-flight state now, then stop accepting new tasks and wait for this "
        "session to end."
    )


def test_control_clear_fresh_is_injected():
    now = 1_000_000
    bridge_start = now - 100
    text = _handle_control("alice", 1, now - 5, "alice", "control:clear", now, bridge_start)
    assert text.startswith("[control:clear from peer=alice] Abort")


def test_control_stale_over_600s_is_dropped():
    now = 1_000_000
    bridge_start = now - 10_000
    text = _handle_control("alice", 1, now - 601, "alice", "control:restart", now, bridge_start)
    assert text is None


def test_control_at_exactly_600s_is_still_fresh():
    now = 1_000_000
    bridge_start = now - 10_000
    text = _handle_control("alice", 1, now - 600, "alice", "control:restart", now, bridge_start)
    assert text is not None


def test_control_older_than_bridge_start_is_dropped_even_if_recent():
    # created is only 5s old (well under the 600s staleness window) but predates
    # this bridge process's own start time -> not meant for this instance.
    now = 1_000_000
    bridge_start = now - 2
    text = _handle_control("alice", 1, now - 5, "alice", "control:restart", now, bridge_start)
    assert text is None


def test_control_unknown_action_is_dropped():
    now = 1_000_000
    bridge_start = now - 100
    text = _handle_control("alice", 1, now - 5, "alice", "control:frobnicate", now, bridge_start)
    assert text is None


def test_control_kind_with_no_action_suffix_is_dropped():
    now = 1_000_000
    bridge_start = now - 100
    text = _handle_control("alice", 1, now - 5, "alice", "control:", now, bridge_start)
    assert text is None


# ---------------------------------------------------------------------------
# group charter injection — inline replica of codex-bridge._get_group_charter
# + the injection-formatting branch of _process_room
# ---------------------------------------------------------------------------

_GROUP_CHARTER_TTL_S = 180


def _get_group_charter(group_name, cache, now, cli_fn):
    cached = cache.get(group_name)
    if cached and now - cached[0] < _GROUP_CHARTER_TTL_S:
        return cached[1]
    out = cli_fn(group_name).strip()
    charter = out if (out and not out.startswith("(no charter set")) else ""
    cache[group_name] = (now, charter)
    return charter


def _build_injection_text(mid, sender, body, group, charter):
    if group:
        prefix = f"[group={group} peer={sender} msg_id={mid}]"
        return f"{prefix} [group charter] {charter}\n{body}" if charter else f"{prefix} {body}"
    return f"[peer={sender} msg_id={mid}] {body}"


def test_charter_injected_for_group_message():
    cache = {}
    calls = []

    def cli_fn(name):
        calls.append(name)
        return "只给结论，不超过 3 行"

    charter = _get_group_charter("team", cache, now=1000, cli_fn=cli_fn)
    text = _build_injection_text(5, "alice", "hi group", "team", charter)
    assert text == "[group=team peer=alice msg_id=5] [group charter] 只给结论，不超过 3 行\nhi group"
    assert calls == ["team"]


def test_no_charter_placeholder_treated_as_empty():
    cache = {}
    charter = _get_group_charter("team", cache, now=1000, cli_fn=lambda name: "(no charter set for team@proj)")
    text = _build_injection_text(5, "alice", "hi group", "team", charter)
    assert text == "[group=team peer=alice msg_id=5] hi group"


def test_dm_never_gets_charter_prefix():
    # 1:1 path never even looks up a charter (group=None) — no CLI call site exists.
    text = _build_injection_text(5, "alice", "hi", None, "")
    assert text == "[peer=alice msg_id=5] hi"


def test_charter_lookup_is_cached_within_ttl():
    cache = {}
    calls = []

    def cli_fn(name):
        calls.append(name)
        return "be terse"

    _get_group_charter("team", cache, now=1000, cli_fn=cli_fn)
    _get_group_charter("team", cache, now=1000 + 60, cli_fn=cli_fn)   # within TTL
    _get_group_charter("team", cache, now=1000 + 179, cli_fn=cli_fn)  # still within TTL
    assert len(calls) == 1, "charter should be served from cache within the TTL window"


def test_charter_lookup_refetches_after_ttl_expires():
    cache = {}
    calls = []

    def cli_fn(name):
        calls.append(name)
        return "be terse"

    _get_group_charter("team", cache, now=1000, cli_fn=cli_fn)
    _get_group_charter("team", cache, now=1000 + 181, cli_fn=cli_fn)  # past TTL
    assert len(calls) == 2


def test_charter_cache_is_per_group():
    cache = {}
    calls = []

    def cli_fn(name):
        calls.append(name)
        return f"charter for {name}"

    c1 = _get_group_charter("team-a", cache, now=1000, cli_fn=cli_fn)
    c2 = _get_group_charter("team-b", cache, now=1000, cli_fn=cli_fn)
    assert c1 == "charter for team-a"
    assert c2 == "charter for team-b"
    assert calls == ["team-a", "team-b"]
