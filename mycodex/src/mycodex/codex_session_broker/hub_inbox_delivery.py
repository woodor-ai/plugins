"""Cursor reconciliation and notification rendering for hub inbox delivery."""

from __future__ import annotations

from collections import OrderedDict


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
    max_messages: int | None = None,
) -> tuple[list[int], str]:
    if not session.pending:
        return [], ""

    first_id, first = next(iter(session.pending.items()))
    if not first.get("deliver", True):
        return [first_id], ""
    kind = first.get("kind") or ""
    if kind.startswith("control:"):
        # Lifecycle control is no longer transported through agent-meeting.
        # Consume historical control messages without rendering their body so
        # a delayed durable message cannot become an executable instruction.
        return [first_id], ""

    selected = []
    lines = []
    for message_id, message in session.pending.items():
        if max_messages is not None and len(selected) >= max_messages:
            break
        if (message.get("kind") or "").startswith("control:"):
            break
        if not message.get("deliver", True):
            break
        selected.append(message_id)
        sender = message_sender(message)
        lines.append(
            f"📬 New Message from {sender} to {session.identity} "
            f"[via woodor:agent-meeting] Message ID: {message_id}"
        )
    return selected, "\n".join(lines)


def build_steer_injection(
    session,
    *,
    max_messages: int,
) -> tuple[list[int], str]:
    """Build one compact working-turn inbox update.

    Bodies remain in am-msgd. The active agent receives exact durable message
    ids and reads each body with ``am message`` at its next safe checkpoint.
    """
    if not session.pending:
        return [], ""
    first_id, first = next(iter(session.pending.items()))
    if not first.get("deliver", True):
        return [first_id], ""
    if (first.get("kind") or "").startswith("control:"):
        return [first_id], ""

    selected: list[int] = []
    senders: OrderedDict[str, int] = OrderedDict()
    for message_id, message in session.pending.items():
        if len(selected) >= max_messages:
            break
        if (message.get("kind") or "").startswith("control:"):
            break
        if not message.get("deliver", True):
            break
        selected.append(message_id)
        sender = message_sender(message)
        senders[sender] = senders.get(sender, 0) + 1

    if not selected:
        return [], ""
    if len(selected) == 1:
        message_id = selected[0]
        message = session.pending[message_id]
        return (
            selected,
            f"📬 New Message from {message_sender(message)} to "
            f"{session.identity} [via woodor:agent-meeting] "
            f"Message ID: {message_id}",
        )

    sender_summary = ", ".join(
        f"{sender} ({count})" for sender, count in senders.items()
    )
    ids = ", ".join(str(message_id) for message_id in selected)
    text = (
        f"📬 {len(selected)} queued messages arrived while "
        f"{session.identity} is working [via woodor:agent-meeting].\n"
        f"Senders: {sender_summary}\n"
        f"Message IDs: {ids}\n"
        "Continue the current task unless a message is urgent. At the next safe "
        "checkpoint, read each exact message by ID before acting on it."
    )
    return selected, text
