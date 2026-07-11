"""
Regression tests for codex-bridge.py's blocked-room retry mechanism
(agent-meeting v0.8.52 — bug#1: injection failure no longer reorders/replays
messages).

codex-bridge.py parses argv at import time, so it is not directly importable
(same constraint as test_bridge_logic.py / test_codex_bridge_control_charter.py).
These tests are an inline replica of the real state machine: _process_room /
_worker / _retry_worker, minus the WS/subprocess/asyncio plumbing, driven
synchronously (no real threads/timers) so the tests are fast and deterministic.
A change to the real logic without a matching update here should be easy to
spot in review.
"""

_MAX_MSG_RETRY_ROUNDS = 5


class FakeBridge:
    """Inline replica of the module-level state + _process_room/_worker/
    _retry_worker in codex-bridge.py. `store` simulates the persisted message
    log (what `meeting read --since N` would return)."""

    def __init__(self, store):
        self.store = store  # room -> list of (mid, sender, body) ascending
        self.cursors = {}
        self.session_cursors = {}
        self.known_peers = set()
        self.peer_blocked = set()
        self.retry_counts = {}
        self.queue = []  # FIFO: list of (room, mid, sender, text)
        self.injected = []  # log of (room, mid, text) actually attempted by the worker
        self.inject_result = lambda room, mid, text: True  # override per test

    # --- inline replica of _fetch_new_messages (peer-only, no first-contact gating) ---
    def _fetch(self, room, since):
        rows = [r for r in self.store.get(room, []) if r[0] > since]
        new_horizon = max([since] + [r[0] for r in rows])
        return rows, new_horizon

    # --- inline replica of _process_room ---
    def process_room(self, room):
        known = room in self.known_peers
        since = max(self.session_cursors.get(room, 0), self.cursors.get(room, 0))
        rows, new_horizon = self._fetch(room, since)
        self.session_cursors[room] = new_horizon
        self.known_peers.add(room)
        for mid, sender, body in rows:
            text = f"[peer={sender} msg_id={mid}] {body}"
            retry_count = self.retry_counts.get((room, mid), 0)
            if retry_count >= _MAX_MSG_RETRY_ROUNDS:
                text = (f"[peer={sender} msg_id={mid}] bridge notice: this message "
                        f"failed to inject {retry_count} times in a row and is being "
                        f"skipped -- run `meeting show self {sender}` to read the "
                        f"original body directly.")
            self.queue.append((room, mid, sender, text))

    # --- inline replica of _worker's per-item handling (one dequeue) ---
    def worker_step(self):
        room, mid, sender, text = self.queue.pop(0)
        if room in self.peer_blocked:
            return  # dropped: frozen room, will be re-fetched once unblocked
        delivered = self.inject_result(room, mid, text)
        if delivered:
            self.injected.append((room, mid, text))
            self.cursors[room] = max(self.cursors.get(room, 0), mid)
            self.retry_counts.pop((room, mid), None)
        else:
            self.peer_blocked.add(room)
            self.retry_counts[(room, mid)] = self.retry_counts.get((room, mid), 0) + 1

    def drain_queue(self):
        while self.queue:
            self.worker_step()

    # --- inline replica of one _retry_worker tick (no sleep) ---
    def retry_cycle(self):
        rooms = list(self.peer_blocked)
        for r in rooms:
            self.session_cursors.pop(r, None)
        self.peer_blocked.difference_update(rooms)
        for room in rooms:
            self.process_room(room)


# ---------------------------------------------------------------------------
# Core requirement: one failed message must not reorder or duplicate later
# messages; the blocked room drops queued traffic until retry re-fetches it.
# ---------------------------------------------------------------------------

def test_failed_message_blocks_room_then_retry_delivers_in_order_once():
    store = {"alice": [(1, "alice", "A"), (2, "alice", "B"), (3, "alice", "C")]}
    b = FakeBridge(store)

    # msg A arrives, fails on injection.
    fail_once = {"done": False}

    def inject(room, mid, text):
        if mid == 1 and not fail_once["done"]:
            fail_once["done"] = True
            return False
        return True

    b.inject_result = inject

    b.process_room("alice")           # enqueues A
    b.worker_step()                   # A fails -> alice blocked
    assert b.peer_blocked == {"alice"}
    assert b.cursors == {}

    # msg B, C arrive while alice is blocked.
    b.process_room("alice")           # enqueues B, C (worker will drop them)
    b.drain_queue()
    assert b.injected == [], "B and C must not be injected while the room is blocked"
    assert b.cursors == {}, "cursor must not move while blocked"

    # retry cycle: unblocks, re-fetches from the committed cursor (0), re-enqueues A,B,C.
    b.retry_cycle()
    b.drain_queue()

    assert [mid for _, mid, _ in b.injected] == [1, 2, 3], (
        "A, B, C must each be injected exactly once, in order, after the retry"
    )
    assert b.cursors["alice"] == 3
    assert b.peer_blocked == set()
    assert b.retry_counts == {}, "retry counters must be cleared once messages succeed"


# ---------------------------------------------------------------------------
# Poison-message ceiling: a message that fails every round must not block the
# room forever -- after _MAX_MSG_RETRY_ROUNDS it is replaced by a short
# stand-in notice, which unblocks the room once it injects successfully.
# ---------------------------------------------------------------------------

def test_poison_message_gets_stand_in_after_max_retry_rounds():
    store = {"alice": [(1, "alice", "x" * 5000)]}
    b = FakeBridge(store)

    original_attempts = []
    stand_in_attempts = []

    def inject(room, mid, text):
        if "bridge notice" in text:
            stand_in_attempts.append(text)
            return True  # the short notice always fits and injects fine
        original_attempts.append(text)
        return False  # the oversized original always fails

    b.inject_result = inject

    b.process_room("alice")
    b.drain_queue()  # first attempt (round 1) fails

    # Drive retry cycles until the stand-in is used and succeeds.
    for _ in range(_MAX_MSG_RETRY_ROUNDS):
        b.retry_cycle()
        b.drain_queue()

    assert len(original_attempts) == _MAX_MSG_RETRY_ROUNDS, (
        f"original body should be retried exactly {_MAX_MSG_RETRY_ROUNDS} times "
        f"before giving up, got {len(original_attempts)}"
    )
    assert len(stand_in_attempts) == 1, "exactly one stand-in notice should be injected"
    assert "meeting show" in stand_in_attempts[0]
    assert "msg_id=1" in stand_in_attempts[0]

    # Cursor must advance past the poison message, room must unblock, and the
    # retry counter must be cleared (no leak).
    assert b.cursors.get("alice") == 1
    assert b.peer_blocked == set()
    assert b.retry_counts == {}


def test_poison_message_does_not_block_later_messages_forever():
    """A poison first message must not starve a normal message queued behind it."""
    store = {"alice": [(1, "alice", "x" * 5000), (2, "alice", "hello")]}
    b = FakeBridge(store)

    def inject(room, mid, text):
        if mid == 1 and "bridge notice" not in text:
            return False  # msg 1's real body always fails
        return True  # msg 1's stand-in and msg 2 both inject fine

    b.inject_result = inject

    b.process_room("alice")   # enqueues msg 1 (msg 2 is already in the log but > since=0,
                               # so it gets fetched here too -- worker will just drop it
                               # while alice is blocked on msg 1)
    b.drain_queue()           # round 1: msg 1 fails (blocks alice), msg 2 dropped (blocked)

    for _ in range(_MAX_MSG_RETRY_ROUNDS):
        b.retry_cycle()
        b.drain_queue()

    assert b.cursors.get("alice") == 2, "msg 2 must eventually be delivered, not starved forever"
    assert b.peer_blocked == set()
