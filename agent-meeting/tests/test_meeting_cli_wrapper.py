"""Regression tests for calling the installed `meeting` runtime wrapper."""

import importlib.util
import sys
from pathlib import Path

import pytest


COMMON_PATH = Path(__file__).resolve().parents[1] / "bin" / "meeting_common.py"
_spec = importlib.util.spec_from_file_location("meeting_common_for_wrapper_test", COMMON_PATH)
meeting_common = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(meeting_common)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell wrapper only")
def test_python_argument_does_not_parse_posix_cli_wrapper_as_python(tmp_path):
    """The runtime `meeting` file is `#!/bin/sh`, not Python source on macOS."""
    wrapper = tmp_path / "meeting"
    wrapper.write_text("#!/bin/sh\nprintf 'wrapper:%s' \"$1\"\n", encoding="utf-8")
    wrapper.chmod(0o755)

    result = meeting_common.run_meeting_cli(
        wrapper, "probe", python=sys.executable, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "wrapper:probe"


def test_python_argument_still_runs_extensionless_python_cli(tmp_path):
    cli = tmp_path / "meeting"
    cli.write_text("import sys\nprint('python:' + sys.argv[1])\n", encoding="utf-8")

    result = meeting_common.run_meeting_cli(
        cli, "probe", python=sys.executable, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "python:probe\n"
