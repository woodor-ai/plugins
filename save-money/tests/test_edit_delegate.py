#!/usr/bin/env python3
"""
Unit tests for cost-edit-delegate hook.py

Runs fully isolated in temp directories; never touches live files.
Run: python3 test_edit_delegate.py
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
_HOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "edit-delegate.py")
_spec = importlib.util.spec_from_file_location("hook", _HOOK_PATH)
_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(tmp_dir, enabled=True):
    cfg = {"edit_delegate": {"enabled": enabled}}
    path = os.path.join(tmp_dir, "cost-opt.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def run_hook(stdin_dict, config_path, env=None):
    """
    Run hook.main() with patched CONFIG_PATH, stdin, and optionally env.
    Returns the parsed JSON printed to stdout, or None if nothing was printed.
    """
    output_buf = io.StringIO()
    fake_stdin = io.StringIO(json.dumps(stdin_dict))
    env_patch = patch.dict(os.environ, env or {}, clear=False)
    with patch.object(_hook, "CONFIG_PATH", config_path), \
         patch("sys.stdin", fake_stdin), \
         patch("sys.stdout", output_buf), \
         env_patch:
        try:
            _hook.main()
        except SystemExit:
            pass

    output = output_buf.getvalue().strip()
    if output:
        return json.loads(output)
    return None


def edit_stdin(tool_name="Edit", file_path="/some/file.py", agent_id=None):
    """Build a PreToolUse stdin dict for an Edit/Write call."""
    d = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "session_id": "test-session",
    }
    if agent_id is not None:
        d["agent_id"] = agent_id
    return d


def non_edit_stdin(tool_name="Bash"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": "echo hi"},
        "session_id": "test-session",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestCostEditDelegate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_config(self.tmp, enabled=True)
        # Make sure the escape-hatch env var never leaks in from the real
        # environment and skews a test.
        os.environ.pop("CLAUDE_ALLOW_MAIN_EDIT", None)

    # Case 1: main agent (no agent_id) + Edit + gate on (explicit true) → deny
    def test_main_agent_edit_is_denied(self):
        result = run_hook(edit_stdin("Edit"), self.cfg)
        self.assertIsNotNone(result)
        decision = result["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("subagent", reason)

    # Case 2: main agent + Write + gate on → deny
    def test_main_agent_write_is_denied(self):
        result = run_hook(edit_stdin("Write"), self.cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 3: subagent (has agent_id) + Edit → allow (deadlock guard)
    def test_subagent_edit_is_allowed(self):
        result = run_hook(edit_stdin("Edit", agent_id="sub-123"), self.cfg)
        self.assertIsNone(result)

    # Case 4: explicit edit_delegate.enabled=false → main agent + Edit → allow
    def test_disabled_allows_main_agent_edit(self):
        cfg_off = make_config(self.tmp, enabled=False)
        result = run_hook(edit_stdin("Edit"), cfg_off)
        self.assertIsNone(result)

    # Case 5: escape hatch env var → main agent + Edit → allow
    def test_env_escape_hatch_allows_main_agent_edit(self):
        result = run_hook(edit_stdin("Edit"), self.cfg, env={"CLAUDE_ALLOW_MAIN_EDIT": "1"})
        self.assertIsNone(result)

    # Case 6: non-Edit/Write tool (Read) → allow
    def test_read_tool_is_allowed(self):
        result = run_hook(non_edit_stdin("Read"), self.cfg)
        self.assertIsNone(result)

    # Case 6b: non-Edit/Write tool (Bash) → allow
    def test_bash_tool_is_allowed(self):
        result = run_hook(non_edit_stdin("Bash"), self.cfg)
        self.assertIsNone(result)

    # Case 7: missing config → opt-out default on → main agent + Edit → deny
    def test_missing_config_defaults_on(self):
        missing = os.path.join(self.tmp, "nonexistent.json")
        result = run_hook(edit_stdin("Edit"), missing)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 8: config present but no edit_delegate key → default on → deny
    def test_missing_key_defaults_on(self):
        cfg_no_key = os.path.join(self.tmp, "cost-opt-other.json")
        with open(cfg_no_key, "w") as f:
            json.dump({"image_delegate": {"enabled": True}}, f)
        result = run_hook(edit_stdin("Edit"), cfg_no_key)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 9: edit_delegate present but no enabled key → default on → deny
    def test_missing_enabled_key_defaults_on(self):
        cfg_no_enabled = os.path.join(self.tmp, "cost-opt-no-enabled.json")
        with open(cfg_no_enabled, "w") as f:
            json.dump({"edit_delegate": {}}, f)
        result = run_hook(edit_stdin("Edit"), cfg_no_enabled)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 10: bad JSON config → default on → deny
    def test_bad_json_config_defaults_on(self):
        bad_cfg = os.path.join(self.tmp, "bad.json")
        with open(bad_cfg, "w") as f:
            f.write("{not valid json:::}")
        result = run_hook(edit_stdin("Edit"), bad_cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 11: edit_delegate section is null → treated as enabled (not explicit false) → deny
    def test_null_section_defaults_on(self):
        null_cfg = os.path.join(self.tmp, "cost-opt-null.json")
        with open(null_cfg, "w") as f:
            json.dump({"edit_delegate": None}, f)
        result = run_hook(edit_stdin("Edit"), null_cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 12: subagent + Write also allowed
    def test_subagent_write_is_allowed(self):
        result = run_hook(edit_stdin("Write", agent_id="sub-456"), self.cfg)
        self.assertIsNone(result)

    # Case 13: main agent writes the handoff card (relative path) → allowed
    def test_main_agent_handoff_card_relative_path_is_allowed(self):
        result = run_hook(
            edit_stdin("Write", file_path=".claude/handoff-pending.md"), self.cfg
        )
        self.assertIsNone(result)

    # Case 14: main agent writes the handoff card (absolute path) → allowed
    def test_main_agent_handoff_card_absolute_path_is_allowed(self):
        result = run_hook(
            edit_stdin(
                "Write",
                file_path="/Users/x/AIAgent/proj/.claude/handoff-pending.md",
            ),
            self.cfg,
        )
        self.assertIsNone(result)

    # Case 15: main agent writes a memory file (~/.claude/projects/.../memory/foo.md) → allowed
    def test_main_agent_memory_file_is_allowed(self):
        path = os.path.expanduser(
            "~/.claude/projects/-Users-x-proj/memory/foo.md"
        )
        result = run_hook(edit_stdin("Write", file_path=path), self.cfg)
        self.assertIsNone(result)

    # Case 16: main agent writes MEMORY.md index → allowed
    def test_main_agent_memory_index_is_allowed(self):
        path = os.path.expanduser(
            "~/.claude/projects/-Users-x-proj/memory/MEMORY.md"
        )
        result = run_hook(edit_stdin("Write", file_path=path), self.cfg)
        self.assertIsNone(result)

    # Case 17: main agent writes a normal code file → still denied (regression)
    def test_main_agent_normal_file_still_denied(self):
        result = run_hook(
            edit_stdin("Write", file_path="/Users/x/proj/src/main.py"), self.cfg
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # Case 18: subagent writes a normal code file → still allowed (original exemption unaffected)
    def test_subagent_normal_file_still_allowed(self):
        result = run_hook(
            edit_stdin(
                "Write", file_path="/Users/x/proj/src/main.py", agent_id="sub-789"
            ),
            self.cfg,
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log_path = "/tmp/cost-edit-delegate-tests.log"
    with open(log_path, "w") as log_file:
        runner = unittest.TextTestRunner(stream=log_file, verbosity=2)
        result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestCostEditDelegate))

    if result.wasSuccessful():
        print(f"PASS: all {result.testsRun} tests")
    else:
        print(f"FAIL: {len(result.failures)} failures, {len(result.errors)} errors")
        print(f"log: {log_path}")
        with open(log_path) as f:
            lines = f.readlines()
        print("".join(lines[-80:]))
        sys.exit(1)
