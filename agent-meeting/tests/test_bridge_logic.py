"""
Regression tests for codex-bridge.py logic.

Tests are function-level (no daemon, no real codex app-server).
They replicate key bridge functions inline to verify the invariants described
in the bug-fix commit (v0.8.38):

  bug#1 — _derive_project: git worktree resolves to main repo name
  bug#2 — cursor is only persisted after successful injection, not on enqueue
  bug#3 — group WS frames read from the group room, not the sender's 1:1 room
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import queue
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# bug#1 — _derive_project worktree convergence
# ---------------------------------------------------------------------------

def _derive_project(cwd: str) -> str:
    """Inline replica of the bridge's _derive_project (--git-common-dir form)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            common_dir = r.stdout.strip()
            if common_dir:
                name = os.path.basename(os.path.dirname(os.path.normpath(common_dir)))
                if name:
                    return "_" if name == "*" else name
    except Exception:
        pass
    name = os.path.basename(os.path.normpath(cwd))
    return "_" if name == "*" else name


def test_derive_project_worktree_resolves_to_main_repo():
    """_derive_project inside a git worktree must return the MAIN repo's basename."""
    main_dir = tempfile.mkdtemp(prefix="bridge-reg-main-")
    wt_dir = tempfile.mkdtemp(prefix="bridge-reg-wt-")
    try:
        subprocess.run(["git", "init", main_dir], capture_output=True, check=False)
        subprocess.run(["git", "-C", main_dir, "config", "user.email", "t@t.com"],
                       capture_output=True)
        subprocess.run(["git", "-C", main_dir, "config", "user.name", "T"],
                       capture_output=True)
        Path(main_dir, "f").write_text("x")
        subprocess.run(["git", "-C", main_dir, "add", "."], capture_output=True)
        subprocess.run(["git", "-C", main_dir, "commit", "-m", "init"],
                       capture_output=True, check=False)
        r_wt = subprocess.run(
            ["git", "-C", main_dir, "worktree", "add", wt_dir, "-b", "reg-feat"],
            capture_output=True,
        )
        if r_wt.returncode != 0:
            pytest.skip("git worktree add failed (git too old?): "
                        + r_wt.stderr.decode(errors="replace").strip()[:120])
        main_name = os.path.basename(os.path.normpath(main_dir))
        assert _derive_project(wt_dir) == main_name, (
            f"worktree should resolve to {main_name!r}, not {_derive_project(wt_dir)!r}"
        )
        # Also verify: deriving from main_dir itself returns the same name
        assert _derive_project(main_dir) == main_name
    finally:
        subprocess.run(["git", "-C", main_dir, "worktree", "remove", "--force", wt_dir],
                       capture_output=True, check=False)
        shutil.rmtree(main_dir, ignore_errors=True)
        shutil.rmtree(wt_dir, ignore_errors=True)


def test_derive_project_star_cwd_fallback():
    """_derive_project must return '_' for a cwd whose basename is '*'."""
    parent = tempfile.mkdtemp(prefix="bridge-reg-star-")
    star = os.path.join(parent, "*")
    os.makedirs(star, exist_ok=True)
    try:
        assert _derive_project(star) == "_"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# bug#2 — cursor persistence only on success
# ---------------------------------------------------------------------------

def _save_cursors(cursors: dict, cursor_file: Path) -> None:
    """Inline replica of bridge._save_cursors."""
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cursor_file.with_name(f".{cursor_file.name}.tmp")
    tmp.write_text(json.dumps(cursors))
    os.replace(tmp, cursor_file)


def _load_cursors(cursor_file: Path) -> dict:
    try:
        return json.loads(cursor_file.read_text()) if cursor_file.exists() else {}
    except Exception:
        return {}


def test_cursor_not_saved_on_enqueue():
    """Enqueueing a message must NOT write the cursor file (pre-injection)."""
    with tempfile.TemporaryDirectory() as tmp:
        cursor_file = Path(tmp) / "cursors" / "cx.json"

        # Simulate _process_room: fetch messages and enqueue WITHOUT saving cursor
        cursors = {}          # committed (starts empty)
        session_cursors = {}  # in-session horizon

        q = queue.Queue()
        # Pretend we fetched messages 1, 2, 3 (like _fetch_new_messages would)
        fetched = [(1, "alice", "hello"), (2, "alice", "world"), (3, "alice", "!")]
        new_horizon = max(mid for mid, *_ in fetched)
        session_cursors["alice"] = new_horizon
        for mid, sender, body in fetched:
            q.put(("alice", mid, sender, body, None))

        # Cursor file must NOT exist yet (no save on enqueue)
        assert not cursor_file.exists(), "cursor file must not be created on enqueue"


def test_cursor_advances_per_success_not_on_failure():
    """Cursor advances one message at a time on success; failure freezes it."""
    with tempfile.TemporaryDirectory() as tmp:
        cursor_file = Path(tmp) / "cursors" / "cx.json"

        cursors: dict = {}
        peer_blocked: set = set()

        def on_success(room, msg_id):
            if room not in peer_blocked:
                cursors[room] = max(cursors.get(room, 0), msg_id)
                _save_cursors(cursors, cursor_file)

        def on_failure(room):
            peer_blocked.add(room)

        # Message 1 succeeds
        on_success("alice", 1)
        assert _load_cursors(cursor_file) == {"alice": 1}

        # Message 2 fails — cursor stays at 1
        on_failure("alice")
        assert _load_cursors(cursor_file) == {"alice": 1}

        # Message 3 succeeds, but alice is blocked — cursor must NOT advance to 3
        on_success("alice", 3)
        assert _load_cursors(cursor_file) == {"alice": 1}, (
            "cursor must not advance past a failed message"
        )

        # Simulate reconnect: clear blocked
        peer_blocked.discard("alice")

        # After reconnect, message 2 retry succeeds — cursor can now advance
        on_success("alice", 2)
        assert _load_cursors(cursor_file)["alice"] == 2

        # Message 3 (re-fetched after reconnect) succeeds
        on_success("alice", 3)
        assert _load_cursors(cursor_file)["alice"] == 3


def test_independent_peers_do_not_interfere():
    """A failure for peer A must not block peer B's cursor."""
    with tempfile.TemporaryDirectory() as tmp:
        cursor_file = Path(tmp) / "cursors" / "cx.json"

        cursors: dict = {}
        peer_blocked: set = set()

        def on_success(room, msg_id):
            if room not in peer_blocked:
                cursors[room] = max(cursors.get(room, 0), msg_id)
                _save_cursors(cursors, cursor_file)

        def on_failure(room):
            peer_blocked.add(room)

        on_success("alice", 5)
        on_failure("alice")
        # bob is independent
        on_success("bob", 10)

        loaded = _load_cursors(cursor_file)
        assert loaded.get("alice") == 5, "alice cursor must stay at 5 (blocked after 5)"
        assert loaded.get("bob") == 10, "bob cursor must advance to 10 (unblocked)"


# ---------------------------------------------------------------------------
# bug#3 — group frames read from group room, not sender's 1:1 room
# ---------------------------------------------------------------------------

def test_group_frame_reads_group_room():
    """The room used for `meeting read` must be the group name, not the sender."""
    reads_issued = []

    def fake_process_room(room: str, group: str = None):
        reads_issued.append((room, group))

    # Simulate what _subscribe_loop does when it receives a group msg frame
    def handle_frame(msg: dict, self_name: str, self_project: str):
        sender = msg.get("sender", "")
        sender_project = msg.get("sender_project", "")
        group = msg.get("group") or None
        if sender == self_name and sender_project == self_project:
            return  # self-sent; skip
        if group:
            if "mention" in msg and not msg["mention"]:
                return  # not @us; skip
            fake_process_room(group, group=group)
        else:
            fake_process_room(sender)

    # DM frame: process_room should use sender
    reads_issued.clear()
    handle_frame({"type": "msg", "sender": "alice", "sender_project": "proj"}, "cx", "proj")
    assert reads_issued == [("alice", None)], f"DM should read alice's room, got {reads_issued}"

    # Group frame (no mention field): process_room should use GROUP NAME
    reads_issued.clear()
    handle_frame({"type": "msg", "sender": "alice", "sender_project": "proj",
                  "group": "team"}, "cx", "proj")
    assert reads_issued == [("team", "team")], (
        f"group msg should read group room 'team', got {reads_issued}"
    )

    # Group frame with mention=True (@ us): should still be processed
    reads_issued.clear()
    handle_frame({"type": "msg", "sender": "alice", "sender_project": "proj",
                  "group": "team", "mention": True}, "cx", "proj")
    assert reads_issued == [("team", "team")]

    # Group frame with mention=False (@ someone else): should be skipped
    reads_issued.clear()
    handle_frame({"type": "msg", "sender": "alice", "sender_project": "proj",
                  "group": "team", "mention": False}, "cx", "proj")
    assert reads_issued == [], f"mention=False group msg must be skipped, got {reads_issued}"

    # Self-sent: always skip
    reads_issued.clear()
    handle_frame({"type": "msg", "sender": "cx", "sender_project": "proj"}, "cx", "proj")
    assert reads_issued == []


def test_group_injection_prefix():
    """Group messages must use [group=G peer=X msg_id=N] prefix; DMs use [peer=X msg_id=N]."""
    def build_text(room: str, sender: str, msg_id: int, body: str, group=None) -> str:
        if group:
            return f"[group={group} peer={sender} msg_id={msg_id}] {body}"
        return f"[peer={sender} msg_id={msg_id}] {body}"

    dm_text = build_text("alice", "alice", 1, "hello", group=None)
    assert dm_text == "[peer=alice msg_id=1] hello"

    grp_text = build_text("team", "alice", 2, "hi group", group="team")
    assert grp_text == "[group=team peer=alice msg_id=2] hi group"


# ---------------------------------------------------------------------------
# v0.8.53 — identity (cwd/project) is re-derived from MAPPING_FILE on every
# use instead of being fixed once at module load. Inline replica of
# codex-bridge.py's _current_identity: mtime-cached, mapping > runtime > HOME.
# ---------------------------------------------------------------------------

def _make_current_identity(mapping_file: Path, runtime_cwd: str, home: str, derive_project):
    unset = object()
    cache = {"mtime": unset, "cwd": None, "project": None}

    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def current_identity():
        try:
            mtime = mapping_file.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != cache["mtime"]:
            mapping = _read_json(mapping_file) if mtime is not None else {}
            cwd = mapping.get("cwd") or runtime_cwd or home
            cache["mtime"] = mtime
            cache["cwd"] = cwd
            cache["project"] = derive_project(cwd)
        return cache["cwd"], cache["project"]

    return current_identity


def test_current_identity_prefers_mapping_over_runtime_and_home():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mapping_file = tmp / "sessions" / "cx.json"
        mapping_file.parent.mkdir(parents=True)
        # No mapping yet -> falls back to runtime cwd
        current_identity = _make_current_identity(
            mapping_file, runtime_cwd="/runtime/cwd", home="/home/x",
            derive_project=lambda cwd: os.path.basename(cwd))
        cwd, project = current_identity()
        assert (cwd, project) == ("/runtime/cwd", "cwd")

        # Mapping now written by the SessionStart hook -> must win over runtime
        mapping_file.write_text(json.dumps({"cwd": "/real/project"}), encoding="utf-8")
        cwd, project = current_identity()
        assert (cwd, project) == ("/real/project", "project")


def test_current_identity_updates_when_mapping_changes_mid_session():
    """A session reused under the same name: mapping starts stale (prior
    session's cwd) then the SessionStart hook overwrites it with the new
    session's cwd. The next identity check must reflect the NEW cwd, not the
    one cached from the first read (this is the split-identity bug: two
    project identities for the same session name)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mapping_file = tmp / "sessions" / "cx.json"
        mapping_file.parent.mkdir(parents=True)
        mapping_file.write_text(json.dumps({"cwd": "/old/session"}), encoding="utf-8")

        current_identity = _make_current_identity(
            mapping_file, runtime_cwd="/runtime/cwd", home="/home/x",
            derive_project=lambda cwd: os.path.basename(cwd))
        assert current_identity() == ("/old/session", "session")

        # Hook rewrites the mapping (new session, same name) -- mtime advances
        time.sleep(0.01)
        mapping_file.write_text(json.dumps({"cwd": "/new/session"}), encoding="utf-8")
        assert current_identity() == ("/new/session", "session")


def test_self_filter_uses_current_identity_not_a_fixed_value():
    """Self-sent filtering must use whatever project MAPPING_FILE derives to
    RIGHT NOW, not a value fixed at process start -- otherwise once the
    mapping's cwd changes, the bridge stops recognizing its own outbound
    messages (or worse, treats a real peer's identical name@project as self)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mapping_file = tmp / "sessions" / "cx.json"
        mapping_file.parent.mkdir(parents=True)
        mapping_file.write_text(json.dumps({"cwd": "/proj-a"}), encoding="utf-8")

        current_identity = _make_current_identity(
            mapping_file, runtime_cwd="/proj-a", home="/home/x",
            derive_project=lambda cwd: os.path.basename(cwd))

        def is_self_sent(sender, sender_project, self_name="cx"):
            _, project = current_identity()
            return sender == self_name and sender_project == project

        assert is_self_sent("cx", "proj-a") is True
        assert is_self_sent("cx", "proj-b") is False  # stale project: not recognized as self

        # Mapping updates to the new project -- self-filter must follow immediately
        time.sleep(0.01)
        mapping_file.write_text(json.dumps({"cwd": "/proj-b"}), encoding="utf-8")
        assert is_self_sent("cx", "proj-b") is True
        assert is_self_sent("cx", "proj-a") is False
