#!/usr/bin/env python3
"""
Focused test for the explicit --proj project-identity flow (v0.8.54+).

Covers the packaged project-identity cache and fallback, no am-msgd needed:
  1. proj_cache_set() then derive_project() returns the cached proj for the
     same root (explicit declaration wins over folder-based derivation).
  2. With no cache and no git, derive_project() falls back to a home-relative
     path (starts with '~' on posix when cwd is under $HOME) and never '*'.
  3. proj_cache_get() returns None for a root that was never cached.
  4. validate_proj() accepts a valid value (stripped) and rejects empty,
     whitespace-only, '*', and whitespace/control-char-containing values.
  5. The codex-session --proj cache write is equivalent to `am online
     --proj`'s: proj_cache_set(_project_root(root), validate_proj(x)) makes
     derive_project(root) return x.

Usage:
    python3 agent-meeting/tests/test_proj_flag.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from agent_meeting.messaging import project_identity  # noqa: E402

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        print(f"  PASS  {name}")
        PASS_COUNT += 1
    else:
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        FAIL_COUNT += 1
        FAILURES.append(msg)


def main():
    meeting_home = tempfile.mkdtemp(prefix="am-projcache-")
    os.environ["MEETING_HOME"] = meeting_home

    def cache_set(root, project):
        project_identity.proj_cache_set(
            root,
            project,
            meeting_home=meeting_home,
        )

    def cache_get(root):
        return project_identity.proj_cache_get(
            root,
            meeting_home=meeting_home,
        )

    def derive(cwd):
        return project_identity.derive_project(
            cwd,
            meeting_home=meeting_home,
        )

    try:
        # (a) proj_cache_set then derive_project returns the cached proj
        root_a = tempfile.mkdtemp(prefix="am-proj-root-a-")
        try:
            cache_set(root_a, "my-explicit-proj")
            got = derive(root_a)
            check("(a) derive_project returns cached explicit proj",
                  got == "my-explicit-proj", f"got {got!r}")
        finally:
            shutil.rmtree(root_a, ignore_errors=True)

        # (b) no cache, no git: derive_project falls back to a home-relative
        # path and never returns '*'
        home = os.path.expanduser("~")
        under_home = tempfile.mkdtemp(prefix="am-proj-under-home-", dir=home)
        try:
            got = derive(under_home)
            check("(b) derive_project never returns '*'", got != "*", f"got {got!r}")
            if not sys.platform.startswith("win"):
                check("(b) derive_project fallback starts with '~' for cwd under $HOME",
                      got.startswith("~"), f"got {got!r}")
        finally:
            shutil.rmtree(under_home, ignore_errors=True)

        # (c) proj_cache_get returns None for an unknown root
        root_c = tempfile.mkdtemp(prefix="am-proj-root-c-")
        try:
            got = cache_get(root_c)
            check("(c) proj_cache_get returns None for uncached root", got is None, f"got {got!r}")
        finally:
            shutil.rmtree(root_c, ignore_errors=True)

        # (d) validate_proj: accepts a valid value, rejects invalid ones
        got = project_identity.validate_project("  my-proj  ")
        check("(d) validate_proj strips a valid value", got == "my-proj", f"got {got!r}")
        for bad in ("", "   ", "has space", "has\tcontrol"):
            try:
                project_identity.validate_project(bad)
                check(f"(d) validate_proj rejects {bad!r}", False, "did not raise")
            except ValueError:
                check(f"(d) validate_proj rejects {bad!r}", True)
        # "*" is a valid explicit declaration (composite-key-identity 0.10.0):
        # --proj=* reaches the same project="*" end state as --global, and
        # must not be special-cased out of the explicit-identity path.
        got_star = project_identity.validate_project("*")
        check("(d) validate_proj accepts '*' as an explicit declaration",
              got_star == "*", f"got {got_star!r}")

        # (e) codex-session's --proj cache write == am online --proj's
        root_e = tempfile.mkdtemp(prefix="am-proj-root-e-")
        try:
            proj = project_identity.validate_project("myproj")
            cache_set(project_identity._project_root(root_e), proj)
            got = derive(root_e)
            check("(e) codex-session cache write makes derive_project return the declared proj",
                  got == "myproj", f"got {got!r}")
        finally:
            shutil.rmtree(root_e, ignore_errors=True)
    finally:
        shutil.rmtree(meeting_home, ignore_errors=True)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed  (total {PASS_COUNT + FAIL_COUNT} checks)")
    if FAILURES:
        print("\nFailed checks:")
        for f in FAILURES:
            print(f)
    print(sep)

    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
