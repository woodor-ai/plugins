#!/usr/bin/env python3
"""
Unit tests for cost-truncate-output hook.
All tests are self-contained; no live files are touched.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Load hook module from its file path so no package structure is needed.
_HOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "truncate-output.py")
_spec = importlib.util.spec_from_file_location("hook", _HOOK_PATH)
_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook)


def run_hook(stdin_dict, config_content=None, tmp_dir=None):
    """
    Run hook.main() with mocked stdin and config.
    Returns (stdout_str, exit_code).
    config_content: string to use as config file content, or None to simulate missing file.
    tmp_dir: directory to use for tempfile creation (defaults to system /tmp).
    """
    stdin_json = json.dumps(stdin_dict)

    captured_stdout = io.StringIO()
    exit_code = [0]

    def fake_exit(code=0):
        exit_code[0] = code
        raise SystemExit(code)

    config_patch = None
    if config_content is not None:
        # Write config to a temp file and patch CONFIG_PATH
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tf.write(config_content)
        tf.flush()
        tf.close()
        config_path = tf.name
    else:
        config_path = "/tmp/__nonexistent_config_xyz__.json"

    original_mkstemp = tempfile.mkstemp

    def patched_mkstemp(prefix="", suffix="", dir="/tmp"):
        # Redirect to provided tmp_dir if given
        actual_dir = tmp_dir if tmp_dir else dir
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=actual_dir)

    try:
        with patch("sys.stdin", io.StringIO(stdin_json)), \
             patch("sys.stdout", captured_stdout), \
             patch("sys.exit", side_effect=fake_exit), \
             patch.object(_hook, "CONFIG_PATH", config_path), \
             patch("tempfile.mkstemp", side_effect=patched_mkstemp):
            try:
                _hook.main()
            except SystemExit:
                pass
    finally:
        if config_content is not None:
            try:
                os.unlink(config_path)
            except Exception:
                pass

    return captured_stdout.getvalue(), exit_code[0]


class TestTruncateOutput(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _big_text(self, chars=200_001):
        """Generate a large text string."""
        chunk = "abcdefghij" * 100  # 1000 chars
        reps = (chars // 1000) + 1
        return (chunk * reps)[:chars]

    def _bash_stdin(self, stdout_content):
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat bigfile.txt"},
            "tool_response": {
                "stdout": stdout_content,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }

    def _read_stdin(self, content):
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.txt"},
            "tool_response": content,
        }

    def test_bash_large_truncated(self):
        """Large Bash stdout → updatedToolOutput with truncated text; full content in /tmp."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config, tmp_dir=self.tmp_dir)
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        updated = result["hookSpecificOutput"]["updatedToolOutput"]
        # Truncated stdout is shorter than original
        self.assertLess(len(updated["stdout"]), len(big))
        # Contains pointer line
        self.assertIn("输出过大已截断", updated["stdout"])
        # stderr/interrupted/isImage preserved
        self.assertEqual(updated["stderr"], "")
        self.assertFalse(updated["interrupted"])
        self.assertFalse(updated["isImage"])
        # Pointer embeds a path that exists and contains the full text
        import re
        m = re.search(r"完整内容存于 ([^\s，）]+)", updated["stdout"])
        self.assertIsNotNone(m, "pointer path not found in truncated output")
        tmp_path = m.group(1).rstrip("，）]")
        self.assertTrue(os.path.exists(tmp_path), f"tmp file missing: {tmp_path}")
        with open(tmp_path) as f:
            saved = f.read()
        self.assertEqual(saved, big)

    def test_bash_small_passthrough(self):
        """Small Bash stdout → no updatedToolOutput (exit 0, no output)."""
        small = "hello world"
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._bash_stdin(small), config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_image_passthrough(self):
        """isImage:true Bash response → never truncated even if stdout is huge."""
        big = self._big_text(200_001)
        stdin_data = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "screenshot"},
            "tool_response": {
                "stdout": big,
                "stderr": "",
                "interrupted": False,
                "isImage": True,
            },
        }
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(stdin_data, config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_image_content_block_passthrough(self):
        """tool_response with image content block → passthrough."""
        stdin_data = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/image.png"},
            "tool_response": [
                {"type": "image", "source": {"type": "base64", "data": "AAAA=="}}
            ],
        }
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(stdin_data, config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_disabled_passthrough(self):
        """text_truncate.enabled=false → always passthrough."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": false, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_missing_config_defaults_off(self):
        """Missing config → disabled (opt-in); big text passes through without truncation."""
        big = self._big_text(200_001)
        stdout, code = run_hook(self._bash_stdin(big), config_content=None)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_missing_key_defaults_off(self):
        """Config exists but text_truncate key absent → disabled; big text passes through."""
        big = self._big_text(200_001)
        config = '{"auto_handoff": {"enabled": true}}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_null_section_defaults_off(self):
        """text_truncate: null → disabled; big text passes through."""
        big = self._big_text(200_001)
        config = '{"text_truncate": null}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_explicit_true_truncates(self):
        """text_truncate.enabled=true → big text is truncated."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config, tmp_dir=self.tmp_dir)
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        updated = result["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("输出过大已截断", updated["stdout"])

    def test_explicit_false_passthrough(self):
        """text_truncate.enabled=false → big text passes through."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": false, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._bash_stdin(big), config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_unknown_tool_passthrough(self):
        """Unknown tool structure → passthrough (no updatedToolOutput)."""
        stdin_data = {
            "hook_event_name": "PostToolUse",
            "tool_name": "SomeMCPTool",
            "tool_input": {},
            "tool_response": {"someField": "x" * 200_001},
        }
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(stdin_data, config_content=config)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")

    def test_tmp_file_contains_full_text(self):
        """The /tmp file written by truncation equals the exact original content."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, _ = run_hook(self._bash_stdin(big), config_content=config, tmp_dir=self.tmp_dir)
        result = json.loads(stdout)
        truncated_stdout = result["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        import re
        m = re.search(r"完整内容存于 ([^\s，）\]]+)", truncated_stdout)
        self.assertIsNotNone(m)
        tmp_path = m.group(1)
        with open(tmp_path) as f:
            saved = f.read()
        self.assertEqual(saved, big, "saved /tmp file must equal original full text")

    def test_truncated_has_head_and_tail(self):
        """Truncated output contains the beginning and the end of the original text."""
        # Build text with distinguishable head/tail markers
        head_marker = "HEAD_MARKER_UNIQUE_XYZ"
        tail_marker = "TAIL_MARKER_UNIQUE_ABC"
        # 200k chars total
        middle = "M" * (200_000 - len(head_marker) - len(tail_marker))
        big = head_marker + middle + tail_marker
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, _ = run_hook(self._bash_stdin(big), config_content=config, tmp_dir=self.tmp_dir)
        result = json.loads(stdout)
        truncated = result["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        self.assertIn(head_marker, truncated, "head marker must be in truncated output")
        self.assertIn(tail_marker, truncated, "tail marker must be in truncated output")

    def test_read_tool_large_truncated(self):
        """Large Read tool_response string → updatedToolOutput is truncated string."""
        big = self._big_text(200_001)
        config = '{"text_truncate": {"enabled": true, "threshold_tokens": 25000}}'
        stdout, code = run_hook(self._read_stdin(big), config_content=config, tmp_dir=self.tmp_dir)
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        updated = result["hookSpecificOutput"]["updatedToolOutput"]
        # For Read, updatedToolOutput is the string directly
        self.assertIsInstance(updated, str)
        self.assertIn("输出过大已截断", updated)
        self.assertLess(len(updated), len(big))


if __name__ == "__main__":
    import subprocess, sys as _sys
    log_path = "/tmp/cost-truncate-tests.log"
    print("---- cost-truncate-output tests ----")
    result = subprocess.run(
        [_sys.executable, "-m", "unittest", __file__, "-v"],
        capture_output=True, text=True
    )
    with open(log_path, "w") as f:
        f.write(result.stdout)
        f.write(result.stderr)
    if result.returncode == 0:
        print("PASS: cost-truncate-output")
    else:
        print("FAIL: cost-truncate-output")
        print(f"log: {log_path}")
        print("---- tail ----")
        lines = (result.stdout + result.stderr).splitlines()
        print("\n".join(lines[-40:]))
    _sys.exit(result.returncode)
