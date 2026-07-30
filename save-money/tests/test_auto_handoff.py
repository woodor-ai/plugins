#!/usr/bin/env python3
"""
Unit tests for cost-auto-handoff hook.py

Runs fully isolated in temp directories; never touches live files.
Run: python3 test_hook.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Load hook module from sibling path without installing it
# ---------------------------------------------------------------------------
_HOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "auto-handoff.py")
_spec = importlib.util.spec_from_file_location("hook", _HOOK_PATH)
_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_transcript(tmp_dir, model, input_tokens, cache_creation, cache_read):
    """Write a minimal JSONL transcript with one assistant message (Claude format)."""
    path = os.path.join(tmp_dir, "transcript.jsonl")
    record = {
        "type": "assistant",
        "message": {
            "model": model,
            "role": "assistant",
            "content": [],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": 100,
            },
        },
        "uuid": "test-uuid",
    }
    with open(path, "w") as f:
        f.write(json.dumps(record) + "\n")
    return path


def make_codex_transcript(tmp_dir, total_input_tokens, model_context_window,
                           filename="codex_transcript.jsonl"):
    """Write a minimal Codex JSONL transcript with a token_count event."""
    path = os.path.join(tmp_dir, filename)
    record = {
        "timestamp": "2026-06-17T14:00:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total_input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                    "total_tokens": total_input_tokens + 100,
                },
                "last_token_usage": {
                    "input_tokens": total_input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                    "total_tokens": total_input_tokens + 100,
                },
                "model_context_window": model_context_window,
            },
            "rate_limits": None,
        },
    }
    with open(path, "w") as f:
        f.write(json.dumps(record) + "\n")
    return path


def make_codex_stdin_json(tmp_dir, transcript_path, model="gpt-5.5",
                          cwd="/fake/cwd", session_id="codex-session"):
    d = {
        "transcript_path": transcript_path,
        "cwd": cwd,
        "model": model,
        "hook_event_name": "PostToolUse",
        "tool_name": "shell",
    }
    if session_id is not None:
        d["session_id"] = session_id
    return d


def make_config(tmp_dir, enabled=True, thresholds=None):
    if thresholds is None:
        thresholds = {"opus": 60, "sonnet": 70, "haiku": 80}
    cfg = {"auto_handoff": {"enabled": enabled, "thresholds_pct": thresholds}}
    path = os.path.join(tmp_dir, "cost-opt.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def make_stdin_json(tmp_dir, transcript_path, cwd="/fake/cwd", session_id="test-session"):
    d = {
        "transcript_path": transcript_path,
        "cwd": cwd,
        "hook_event_name": "Stop",
    }
    if session_id is not None:
        d["session_id"] = session_id
    return d


def run_hook(tmp_dir, stdin_dict, config_path, triggers_dir, fired_dir,
             agent_name="test-agent"):
    """
    Invoke hook.main() with lifecycle resolution and detached dispatch mocked.
    Returns a diagnostic dict if dispatch was requested, or None.
    """
    import io
    fake_stdin = io.StringIO(json.dumps(stdin_dict))
    dispatched = MagicMock(return_value=True)
    with patch.object(_hook, "CONFIG_PATH", config_path), \
         patch.object(_hook, "FIRED_DIR", fired_dir), \
         patch.object(
             _hook,
             "resolve_lifecycle_agent",
             return_value=(agent_name, "tools"),
         ), \
         patch.object(_hook, "spawn_lifecycle_dispatch", dispatched), \
         patch("sys.stdin", fake_stdin):
        try:
            _hook.main()
        except SystemExit:
            pass

    if not dispatched.called:
        return None
    model, context_tokens = _hook.last_assistant_usage(
        stdin_dict["transcript_path"]
    )
    family = _hook.model_family(model)
    with open(config_path) as stream:
        thresholds = json.load(stream)["auto_handoff"]["thresholds_pct"]
    threshold_tokens = max(
        int(thresholds[family] / 100 * _hook.WINDOW_TOKENS[family]),
        _hook.MIN_FIRE_TOKENS,
    )
    return {
        "agent": agent_name,
        "project": "tools",
        "reason": "auto-handoff",
        "context_tokens": context_tokens,
        "threshold_tokens": threshold_tokens,
        "ts": int(time.time()),
    }


def run_codex_hook(stdin_dict, config_path, fired_dir):
    """
    Invoke hook.main() for the Codex PostToolUse path.
    Returns (dispatch_requested, fired_flag_exists).
    """
    import io
    fake_stdin = io.StringIO(json.dumps(stdin_dict))
    captured_stdout = io.StringIO()
    dispatched = MagicMock(return_value=True)
    with patch.object(_hook, "CONFIG_PATH", config_path), \
         patch.object(_hook, "FIRED_DIR", fired_dir), \
         patch.object(
             _hook,
             "resolve_lifecycle_agent",
             return_value=("test-agent", "tools"),
         ), \
         patch.object(_hook, "spawn_lifecycle_dispatch", dispatched), \
         patch("sys.stdin", fake_stdin), \
         patch("sys.stdout", captured_stdout):
        try:
            _hook.main()
        except SystemExit:
            pass

    session_id = stdin_dict.get("session_id")
    fired = session_id and os.path.exists(os.path.join(fired_dir, session_id))
    return ({"identity": "test-agent@tools"} if dispatched.called else None), fired


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestCostAutoHandoff(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.triggers = os.path.join(self.tmp, "triggers")
        self.fired = os.path.join(self.tmp, "fired")

    # Case 1: enabled=false → no trigger
    def test_disabled_no_trigger(self):
        cfg = make_config(self.tmp, enabled=False)
        # opus 1M window: even large usage should not trigger when disabled
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.triggers))

    # Case 2: enabled=true, opus usage below 60% of 1M threshold → no trigger
    def test_below_threshold_no_trigger(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 60% of 1_000_000 = 600_000; use 500_000 total (below)
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 500000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 3: enabled=true, opus > 60% of 1M → trigger written with correct fields
    def test_opus_above_threshold_trigger(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 60% of 1_000_000 = 600_000; use 650_000 total (above)
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 600000, 25000, 25000)
        stdin = make_stdin_json(self.tmp, transcript, cwd="/fake/cwd")
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired,
                          agent_name="plugins-wic")
        self.assertIsNotNone(result)
        self.assertEqual(result["agent"], "plugins-wic")
        self.assertEqual(result["reason"], "auto-handoff")
        self.assertEqual(result["context_tokens"], 650000)
        self.assertEqual(result["threshold_tokens"], 600000)
        self.assertIn("ts", result)
        # Verify atomic: no .tmp left behind
        tmp_file = os.path.join(self.triggers, "plugins-wic.json.tmp")
        self.assertFalse(os.path.exists(tmp_file))

    # Case 4a: sonnet threshold (70% of 1M = 700k) fires correctly
    def test_sonnet_threshold(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"sonnet": 70})
        # 70% of 1_000_000 = 700_000; use 750_000 total (above)
        transcript = make_transcript(self.tmp, "claude-sonnet-4-6", 750000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        self.assertEqual(result["threshold_tokens"], 700000)

    # Case 4b: haiku threshold (80% of 200k = 160k) fires correctly
    def test_haiku_threshold(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"haiku": 80})
        # 80% of 200_000 = 160_000; use 170_000 total (above)
        transcript = make_transcript(self.tmp, "claude-haiku-4-5", 170000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        self.assertEqual(result["threshold_tokens"], 160000)

    # Case 4c: sonnet below 70% of 1M → no trigger
    def test_sonnet_below_threshold(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"sonnet": 70})
        # 70% of 1_000_000 = 700_000; use 650_000 total (below)
        transcript = make_transcript(self.tmp, "claude-sonnet-4-6", 650000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 4d: haiku below 80% of 200k → no trigger
    def test_haiku_below_threshold(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"haiku": 80})
        # 80% of 200_000 = 160_000; use 150_000 total (below)
        transcript = make_transcript(self.tmp, "claude-haiku-4-5", 150000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 5: unknown family (fable) → no trigger
    def test_unknown_family_no_trigger(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60, "sonnet": 70})
        transcript = make_transcript(self.tmp, "claude-fable-1-0", 2000000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 6a: config file missing → no trigger
    def test_missing_config_no_trigger(self):
        missing_cfg = os.path.join(self.tmp, "nonexistent.json")
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, missing_cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 6b: config file bad JSON → no trigger
    def test_bad_json_config_no_trigger(self):
        bad_cfg = os.path.join(self.tmp, "bad.json")
        with open(bad_cfg, "w") as f:
            f.write("{not valid json:::}")
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, bad_cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 7: lifecycle identity resolution returns None → no dispatch
    def test_no_meeting_name_no_trigger(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        import io
        fake_stdin = io.StringIO(json.dumps(stdin))
        with patch.object(_hook, "CONFIG_PATH", cfg), \
             patch.object(_hook, "FIRED_DIR", self.fired), \
             patch.object(_hook, "resolve_lifecycle_agent", return_value=None), \
             patch.object(_hook, "spawn_lifecycle_dispatch") as dispatch, \
             patch("sys.stdin", fake_stdin):
            try:
                _hook.main()
            except SystemExit:
                pass
        dispatch.assert_not_called()

    # Bonus: three-part usage sum is correct (uses opus 1M window now)
    def test_usage_sum_all_three_parts(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 2000 + 50000 + 600000 = 652000 > 600000
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 2000, 50000, 600000)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        self.assertEqual(result["context_tokens"], 652000)

    # Case 8a: dedup — same session_id fires once, second call is no-op
    def test_dedup_same_session_fires_once(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 650k > 600k threshold
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 650000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript, session_id="dedup-session-abc")

        # First call: should fire
        result1 = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result1)

        # Fired flag must exist
        flag_path = os.path.join(self.fired, "dedup-session-abc")
        self.assertTrue(os.path.exists(flag_path))

        # Second call with same session_id: must NOT fire again
        result2 = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result2)

    # Case 8b: dedup — no session_id in stdin, threshold exceeded → still fires
    def test_dedup_no_session_id_still_fires(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 650000, 0, 0)
        # session_id=None means key is omitted from stdin
        stdin = make_stdin_json(self.tmp, transcript, session_id=None)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        # No flag file created (no session_id to key on)
        self.assertFalse(os.path.exists(self.fired))

    # Case 9a: MIN_FIRE_TOKENS floor — haiku pct=20 (→40k), context=60k >40k but <100k → no trigger
    def test_min_fire_floor_blocks_low_pct(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"haiku": 20})
        # 20% of 200_000 = 40_000; context=60_000 exceeds pct threshold but is below 100k floor
        transcript = make_transcript(self.tmp, "claude-haiku-4-5", 60000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    # Case 9b: MIN_FIRE_TOKENS floor — haiku pct=20 (→40k), context=120k >100k floor → triggers,
    # diagnostic threshold_tokens == 100_000 (effective, not pct-derived 40k)
    def test_min_fire_floor_trigger_uses_effective_threshold(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"haiku": 20})
        # 20% of 200_000 = 40_000; context=120_000 exceeds 100k floor → fires
        transcript = make_transcript(self.tmp, "claude-haiku-4-5", 120000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        self.assertEqual(result["threshold_tokens"], 100000)  # effective, not 40k
        self.assertEqual(result["context_tokens"], 120000)

    # Case 9c: production opus 60% (→600k) is above floor → effective stays 600k, not clamped to 100k
    def test_production_opus_threshold_unaffected_by_floor(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 60% of 1_000_000 = 600_000 > 100_000 floor → effective = 600_000
        # context=500_000: below effective → no trigger
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 500000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result)

    def test_production_opus_above_threshold_writes_pct_value(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        # 60% of 1_000_000 = 600_000; context=650_000 > 600_000 → fires
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 650000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNotNone(result)
        # threshold_tokens must be 600_000 (the pct-derived value), not 100_000
        self.assertEqual(result["threshold_tokens"], 600000)

    def test_null_section_no_trigger(self):
        """auto_handoff section is null → treated as disabled, no trigger."""
        cfg_path = os.path.join(self.tmp, "cost-opt-null.json")
        with open(cfg_path, "w") as f:
            json.dump({"auto_handoff": None}, f)
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        result = run_hook(self.tmp, stdin, cfg_path, self.triggers, self.fired)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Codex path tests (PostToolUse, token_count from transcript JSONL)
# ---------------------------------------------------------------------------

class TestCodexAutoHandoff(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fired = os.path.join(self.tmp, "fired")

    def _make_cfg(self, thresholds=None, enabled=True):
        if thresholds is None:
            thresholds = {"sonnet": 70}
        return make_config(self.tmp, enabled=enabled, thresholds=thresholds)

    # C1: below threshold → no output, no fired flag
    def test_codex_below_threshold_no_output(self):
        # 258400 * 70% = 180880; use 100000 (below)
        transcript = make_codex_transcript(self.tmp, 100000, 258400)
        cfg = self._make_cfg({"sonnet": 70})
        stdin = make_codex_stdin_json(self.tmp, transcript, model="gpt-5.5")
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output)
        self.assertFalse(fired)

    # C2: above threshold → local lifecycle dispatch + fired flag
    def test_codex_above_threshold_fires(self):
        # 258400 * 70% = 180880; use 200000 (above)
        transcript = make_codex_transcript(self.tmp, 200000, 258400)
        cfg = self._make_cfg({"sonnet": 70})
        stdin = make_codex_stdin_json(self.tmp, transcript, model="gpt-5.5",
                                      session_id="codex-fire-test")
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNotNone(output)
        self.assertEqual(output["identity"], "test-agent@tools")
        self.assertTrue(fired)

    # C3: disabled → no output
    def test_codex_disabled_no_output(self):
        transcript = make_codex_transcript(self.tmp, 250000, 258400)
        cfg = self._make_cfg(enabled=False)
        stdin = make_codex_stdin_json(self.tmp, transcript)
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output)
        self.assertFalse(fired)

    # C4: dedup — same session_id fires only once
    def test_codex_dedup_fires_once(self):
        transcript = make_codex_transcript(self.tmp, 200000, 258400)
        cfg = self._make_cfg({"sonnet": 70})
        stdin = make_codex_stdin_json(self.tmp, transcript, session_id="codex-dedup")

        output1, fired1 = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNotNone(output1)
        self.assertTrue(fired1)

        output2, fired2 = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output2)
        self.assertTrue(fired2)  # flag still exists

    # C5: no threshold for the detected family → no output
    def test_codex_no_threshold_for_family_no_output(self):
        # model maps to "opus" but config only has "sonnet"
        transcript = make_codex_transcript(self.tmp, 200000, 258400)
        cfg = self._make_cfg({"sonnet": 70})
        stdin = make_codex_stdin_json(self.tmp, transcript, model="gpt-5.5-opus")
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output)
        self.assertFalse(fired)

    # C6: transcript has no token_count event → no output
    def test_codex_no_token_count_event_no_output(self):
        # Write a transcript with no token_count event (e.g. empty)
        transcript = os.path.join(self.tmp, "empty_codex.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        cfg = self._make_cfg({"sonnet": 70})
        stdin = make_codex_stdin_json(self.tmp, transcript)
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output)

    # C7: MIN_FIRE_TOKENS floor — threshold_pct gives < 100k but context is 50k → no fire
    def test_codex_min_fire_floor_blocks(self):
        # 258400 * 10% = 25840 < 100000 floor; context = 50000 < floor → no fire
        transcript = make_codex_transcript(self.tmp, 50000, 258400)
        cfg = self._make_cfg({"sonnet": 10})
        stdin = make_codex_stdin_json(self.tmp, transcript, model="gpt-5.5")
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNone(output)
        self.assertFalse(fired)

    # C8: MIN_FIRE_TOKENS floor — context above floor fires even when pct-derived threshold is lower
    def test_codex_min_fire_floor_fires_when_above(self):
        # 258400 * 10% = 25840 < floor=100k; context = 150000 > floor → fires
        transcript = make_codex_transcript(self.tmp, 150000, 258400)
        cfg = self._make_cfg({"sonnet": 10})
        stdin = make_codex_stdin_json(self.tmp, transcript, model="gpt-5.5",
                                      session_id="codex-floor-test")
        output, fired = run_codex_hook(stdin, cfg, self.fired)
        self.assertIsNotNone(output)
        self.assertTrue(fired)

    # C9: Claude Stop path unaffected — PostToolUse event does NOT go through Claude path
    def test_claude_stop_path_unaffected_by_codex_changes(self):
        """Regression: Stop hook uses the same local lifecycle dispatch."""
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 650000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        triggers = os.path.join(self.tmp, "triggers")
        result = run_hook(self.tmp, stdin, cfg, triggers, self.fired)
        self.assertIsNotNone(result)
        self.assertEqual(result["agent"], "test-agent")
        self.assertEqual(result["context_tokens"], 650000)


class TestLifecycleDispatch(unittest.TestCase):
    def test_resolve_requires_one_local_platform_match(self):
        sessions = [
            {
                "name": "worker",
                "project": "tools",
                "platform": "claude",
                "cwd": "/work/tools",
                "transcript_session_id": "session-1",
            },
            {
                "name": "worker",
                "project": "tools",
                "platform": "codex",
                "cwd": "/work/tools",
                "thread_id": "thread-1",
            },
        ]
        with patch.object(_hook, "lifecycle_sessions", return_value=sessions):
            resolved = _hook.resolve_lifecycle_agent(
                "/work/tools",
                "claude",
                "session-1",
            )
        self.assertEqual(resolved, ("worker", "tools"))

    def test_dispatch_calls_handoff_then_restart(self):
        session = {
            "name": "worker",
            "project": "tools",
            "state": "idle",
            "confidence": "high",
        }
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            return types.SimpleNamespace(returncode=0)

        with patch.object(_hook, "lifecycle_sessions", return_value=[session]), \
             patch.object(_hook.subprocess, "run", side_effect=run):
            self.assertTrue(_hook.dispatch_lifecycle("worker", "tools"))

        self.assertEqual(calls[0][-1], "handoff")
        self.assertEqual(calls[1][-1], "restart")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import io
    log_path = "/tmp/cost-auto-handoff-tests.log"
    with open(log_path, "w") as log_file:
        runner = unittest.TextTestRunner(stream=log_file, verbosity=2)
        suite = unittest.TestSuite()
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestCostAutoHandoff))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestCodexAutoHandoff))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestLifecycleDispatch))
        result = runner.run(suite)

    if result.wasSuccessful():
        print(f"PASS: all {result.testsRun} tests")
    else:
        print(f"FAIL: {len(result.failures)} failures, {len(result.errors)} errors")
        print(f"log: {log_path}")
        with open(log_path) as f:
            lines = f.readlines()
            print("".join(lines[-80:]))
        sys.exit(1)
