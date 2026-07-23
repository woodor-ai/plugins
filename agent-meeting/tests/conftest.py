"""This directory holds two unrelated test regimes:

1. Real pytest suites (test_bridge_*.py, test_codex_*.py, test_migration_*.py,
   test_proj_flag.py, test_apply_identity_remap.py, test_e2e_bridge.py) — run
   normally with `pytest agent-meeting/tests/`.

2. Standalone executable scripts whose check functions happen to be named
   `test_*` but take plain positional args, not pytest fixtures. Run each
   directly, not through pytest:

     MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_identity_regression.py
     MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_ws.py
     MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_ws_monitor.py
     MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_authoritative_project.py
     MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_prune.py

   pytest collection must skip these — it would otherwise report
   "fixture 'meeting_home'/'db_dir' not found" for every function, which is
   noise, not a real failure.
"""

collect_ignore = [
    "test_identity_regression.py",
    "test_ws.py",
    "test_ws_monitor.py",
    "test_authoritative_project.py",
    "test_prune.py",
]
