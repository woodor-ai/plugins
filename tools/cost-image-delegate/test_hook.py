#!/usr/bin/env python3
"""
Unit tests for cost-image-delegate hook.py

Runs fully isolated in temp directories; never touches live files.
Run: python3 test_hook.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


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

def make_config(tmp_dir, enabled=True):
    cfg = {"image_delegate": {"enabled": enabled}}
    path = os.path.join(tmp_dir, "cost-opt.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def run_hook(stdin_dict, config_path):
    """
    Run hook.main() with patched CONFIG_PATH and stdin.
    Returns the parsed JSON printed to stdout, or None if nothing was printed.
    """
    output_buf = io.StringIO()
    fake_stdin = io.StringIO(json.dumps(stdin_dict))
    with patch.object(_hook, "CONFIG_PATH", config_path), \
         patch("sys.stdin", fake_stdin), \
         patch("sys.stdout", output_buf):
        try:
            _hook.main()
        except SystemExit:
            pass

    output = output_buf.getvalue().strip()
    if output:
        return json.loads(output)
    return None


def read_stdin(file_path, agent_id=None):
    """Build a PreToolUse stdin dict for a Read call."""
    d = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "session_id": "test-session",
    }
    if agent_id is not None:
        d["agent_id"] = agent_id
    return d


def non_read_stdin(tool_name="Bash"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": "echo hi"},
        "session_id": "test-session",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestCostImageDelegate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_config(self.tmp, enabled=True)

    # Case 1: main agent (no agent_id) reads .png → deny
    def test_main_agent_png_is_denied(self):
        result = run_hook(read_stdin("/some/screenshot.png"), self.cfg)
        self.assertIsNotNone(result)
        decision = result["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("explore subagent", reason)

    # Case 2: subagent (has agent_id) reads same .png → allow (no deny output)
    def test_subagent_png_is_allowed(self):
        result = run_hook(read_stdin("/some/screenshot.png", agent_id="sub-123"), self.cfg)
        self.assertIsNone(result)

    # Case 3: main agent reads non-image (.py) → allow
    def test_main_agent_py_is_allowed(self):
        result = run_hook(read_stdin("/some/script.py"), self.cfg)
        self.assertIsNone(result)

    # Case 3b: main agent reads non-image (.md) → allow
    def test_main_agent_md_is_allowed(self):
        result = run_hook(read_stdin("/some/README.md"), self.cfg)
        self.assertIsNone(result)

    # Case 4: non-Read tool (Bash) → allow
    def test_bash_tool_is_allowed(self):
        result = run_hook(non_read_stdin("Bash"), self.cfg)
        self.assertIsNone(result)

    # Case 5: image_delegate.enabled=false → main agent reads .png → allow
    def test_disabled_allows_main_agent_image(self):
        cfg_off = make_config(self.tmp, enabled=False)
        result = run_hook(read_stdin("/some/photo.jpg"), cfg_off)
        self.assertIsNone(result)

    # Case 6: config missing → opt-in default off → main agent reads .png → allow
    def test_missing_config_defaults_off(self):
        missing = os.path.join(self.tmp, "nonexistent.json")
        result = run_hook(read_stdin("/some/image.webp"), missing)
        self.assertIsNone(result)

    # Case 7: various image extensions — uppercase and mixed case
    def test_uppercase_png_denied(self):
        result = run_hook(read_stdin("/img/photo.PNG"), self.cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_mixed_case_jpeg_denied(self):
        result = run_hook(read_stdin("/img/photo.JPEG"), self.cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gif_denied(self):
        result = run_hook(read_stdin("/img/anim.gif"), self.cfg)
        self.assertIsNotNone(result)

    def test_webp_denied(self):
        result = run_hook(read_stdin("/img/photo.webp"), self.cfg)
        self.assertIsNotNone(result)

    def test_bmp_denied(self):
        result = run_hook(read_stdin("/img/scan.bmp"), self.cfg)
        self.assertIsNotNone(result)

    def test_svg_denied(self):
        result = run_hook(read_stdin("/img/icon.svg"), self.cfg)
        self.assertIsNotNone(result)

    def test_jpg_denied(self):
        result = run_hook(read_stdin("/img/photo.jpg"), self.cfg)
        self.assertIsNotNone(result)

    def test_Mixed_Case_Jpg_denied(self):
        result = run_hook(read_stdin("/img/photo.Jpg"), self.cfg)
        self.assertIsNotNone(result)

    # Case 8: bad JSON config → opt-in default off → main agent reads .png → allow
    def test_bad_json_config_defaults_off(self):
        bad_cfg = os.path.join(self.tmp, "bad.json")
        with open(bad_cfg, "w") as f:
            f.write("{not valid json:::}")
        result = run_hook(read_stdin("/img/photo.png"), bad_cfg)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log_path = "/tmp/cost-image-delegate-tests.log"
    with open(log_path, "w") as log_file:
        runner = unittest.TextTestRunner(stream=log_file, verbosity=2)
        result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestCostImageDelegate))

    if result.wasSuccessful():
        print(f"PASS: all {result.testsRun} tests")
    else:
        print(f"FAIL: {len(result.failures)} failures, {len(result.errors)} errors")
        print(f"log: {log_path}")
        with open(log_path) as f:
            lines = f.readlines()
        print("".join(lines[-80:]))
        sys.exit(1)
