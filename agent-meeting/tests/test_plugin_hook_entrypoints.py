from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def test_claude_session_start_is_a_noop_before_runtime_install(tmp_path):
    script = PRODUCT_ROOT / "scripts" / "claude_session_start.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "MEETING_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert result.stderr == ""
