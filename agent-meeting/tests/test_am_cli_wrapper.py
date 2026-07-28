"""Regression tests for calling the installed `am` runtime wrapper."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


COMMON_PATH = Path(__file__).resolve().parents[1] / "bin" / "am_common.py"
_spec = importlib.util.spec_from_file_location("am_common_for_wrapper_test", COMMON_PATH)
am_common = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(am_common)


def test_removed_am_msgd_subcommand_is_not_accepted():
    command = Path(__file__).resolve().parents[1] / "bin" / "am"
    result = subprocess.run(
        [sys.executable, str(command), "am-msgd", "status"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell wrapper only")
def test_python_argument_does_not_parse_posix_cli_wrapper_as_python(tmp_path):
    """The runtime `am` file is `#!/bin/sh`, not Python source on macOS."""
    wrapper = tmp_path / "am"
    wrapper.write_text("#!/bin/sh\nprintf 'wrapper:%s' \"$1\"\n", encoding="utf-8")
    wrapper.chmod(0o755)

    result = am_common.run_am_cli(
        wrapper, "probe", python=sys.executable, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "wrapper:probe"


def test_python_argument_still_runs_extensionless_python_cli(tmp_path):
    cli = tmp_path / "am"
    cli.write_text("import sys\nprint('python:' + sys.argv[1])\n", encoding="utf-8")

    result = am_common.run_am_cli(
        cli, "probe", python=sys.executable, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "python:probe\n"
