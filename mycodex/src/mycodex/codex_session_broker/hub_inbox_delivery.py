"""Cursor reconciliation and notification rendering for hub inbox delivery."""

from __future__ import annotations

import time


def reconcile_central_cursor(session, central_cursor) -> None:
    central_cursor = int(central_cursor)
    if session.cursor is None:
        session.cursor = central_cursor
        return
    if central_cursor == session.cursor:
        return
    if central_cursor < session.cursor:
        raise RuntimeError(
            "central cursor moved backwards "
            f"({session.cursor} -> {central_cursor})"
        )
    if session.awaiting_ack:
        through = max(session.awaiting_ack)
        if central_cursor >= through:
            for message_id in session.awaiting_ack:
                session.pending.pop(message_id, None)
            session.awaiting_ack = None
            session.cursor = central_cursor
            return
    if not session.pending and not session.awaiting_ack:
        session.cursor = central_cursor
        return
    raise RuntimeError(
        "central cursor diverged while messages were pending "
        f"({session.cursor} -> {central_cursor})"
    )


def message_sender(message) -> str:
    if message.get("sender") and message.get("sender_project"):
        return f"{message['sender']}@{message['sender_project']}"
    return message.get("sender_identity") or message.get("sender")


def build_injection(
    session,
    *,
    control_stale_seconds: int,
    now: int | None = None,
) -> tuple[list[int], str]:
    if not session.pending:
        return [], ""

    first_id, first = next(iter(session.pending.items()))
    if not first.get("deliver", True):
        return [first_id], ""
    kind = first.get("kind") or ""
    if kind.startswith("control:"):
        age = (int(time.time()) if now is None else now) - int(
            first.get("created_at") or 0
        )
        if age > control_stale_seconds:
            return [first_id], ""
        action = kind.split(":", 1)[1]
        directives = {
            "restart": (
                "Write a handoff card summarizing in-flight state now, then stop "
                "accepting new tasks and wait for this session to end."
            ),
            "clear": (
                "Abort whatever task is in flight, clear your working context, "
                "and report back that you have been cleared."
            ),
        }
        directive = directives.get(action)
        if directive is None:
            return [first_id], ""
        sender = message_sender(first)
        return [first_id], (
            f"[control:{action} from peer={sender}] {directive}"
        )

    selected = []
    lines = []
    for message_id, message in session.pending.items():
        if (message.get("kind") or "").startswith("control:"):
            break
        if not message.get("deliver", True):
            break
        selected.append(message_id)
        sender = message_sender(message)
        group = message.get("group")
        ask = str(message.get("ask") or "").replace("\r", " ").replace(
            "\n",
            " ",
        )
        if len(ask) > 100:
            ask = ask[:100] + "..."
        if group:
            notice = (
                f"📬 New Message from {sender} in group {group} "
                "[via woodor:agent-meeting]"
            )
        else:
            notice = (
                f"📬 New Message from {sender} [via woodor:agent-meeting]"
            )
        if ask:
            notice += f": {ask}"
        lines.extend((notice, f"  Message ID: {message_id}"))
    lines.append(f"Agent-meeting recipient: {session.identity}")
    return selected, "\n".join(lines)
