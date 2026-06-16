#!/usr/bin/env python3
"""
Unit tests for cost-auto-handoff hook.py

Runs fully isolated in temp directories; never touches live files.
Run: python3 test_hook.py
"""

import importlib.util
import json
import os
import socket
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
_HOOK_PATH = os.path.join(os.path.dirname(__file__), "hook.py")
_spec = importlib.util.spec_from_file_location("hook", _HOOK_PATH)
_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_transcript(tmp_dir, model, input_tokens, cache_creation, cache_read):
    """Write a minimal JSONL transcript with one assistant message."""
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
    Invoke hook.main() with patched CONFIG_PATH, TRIGGERS_DIR, FIRED_DIR, and
    resolve_agent_name. Returns the trigger file content dict if written,
    or None.
    """
    import io
    fake_stdin = io.StringIO(json.dumps(stdin_dict))
    with patch.object(_hook, "CONFIG_PATH", config_path), \
         patch.object(_hook, "TRIGGERS_DIR", triggers_dir), \
         patch.object(_hook, "FIRED_DIR", fired_dir), \
         patch.object(_hook, "resolve_agent_name", return_value=agent_name), \
         patch("sys.stdin", fake_stdin):
        try:
            _hook.main()
        except SystemExit:
            pass

    trigger_file = os.path.join(triggers_dir, f"{agent_name}.json")
    if os.path.exists(trigger_file):
        with open(trigger_file) as f:
            return json.load(f)
    return None


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

    # Case 7: resolve_agent_name returns None → no trigger
    def test_no_meeting_name_no_trigger(self):
        cfg = make_config(self.tmp, enabled=True, thresholds={"opus": 60})
        transcript = make_transcript(self.tmp, "claude-opus-4-8", 700000, 0, 0)
        stdin = make_stdin_json(self.tmp, transcript)
        import io
        fake_stdin = io.StringIO(json.dumps(stdin))
        with patch.object(_hook, "CONFIG_PATH", cfg), \
             patch.object(_hook, "TRIGGERS_DIR", self.triggers), \
             patch.object(_hook, "FIRED_DIR", self.fired), \
             patch.object(_hook, "resolve_agent_name", return_value=None), \
             patch("sys.stdin", fake_stdin):
            try:
                _hook.main()
            except SystemExit:
                pass
        self.assertFalse(os.path.exists(self.triggers))

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

        # Remove trigger file so we can detect if second call re-creates it
        trigger_file = os.path.join(self.triggers, "test-agent.json")
        os.remove(trigger_file)

        # Second call with same session_id: must NOT fire again
        result2 = run_hook(self.tmp, stdin, cfg, self.triggers, self.fired)
        self.assertIsNone(result2)
        self.assertFalse(os.path.exists(trigger_file))

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import io
    log_path = "/tmp/cost-auto-handoff-tests.log"
    with open(log_path, "w") as log_file:
        runner = unittest.TextTestRunner(stream=log_file, verbosity=2)
        result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestCostAutoHandoff))

    if result.wasSuccessful():
        print(f"PASS: all {result.testsRun} tests")
    else:
        print(f"FAIL: {len(result.failures)} failures, {len(result.errors)} errors")
        print(f"log: {log_path}")
        with open(log_path) as f:
            lines = f.readlines()
            print("".join(lines[-80:]))
        sys.exit(1)
