"""One-to-one conversation commands for the public ``am`` client."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ConversationCommandServices:
    resolve_host: Callable[[str | None], str]
    parse_self: Callable[[str, str | None], tuple[str, str]]
    resolve_peer: Callable[..., tuple[str, str]]
    request: Callable
    record_event: Callable[[str], None]


def _participants(args, services: ConversationCommandServices):
    host = services.resolve_host(getattr(args, "host", None))
    cwd = os.getcwd()
    self_project, self_name = services.parse_self(args.self_arg, cwd)
    peer_project, peer_name = services.resolve_peer(
        host,
        args.peer,
    )
    return (
        host,
        self_project,
        self_name,
        peer_project,
        peer_name,
    )


def send(args, services: ConversationCommandServices) -> None:
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as body_file:
            body = body_file.read().rstrip("\n")
    elif args.body == "-":
        body = sys.stdin.read().rstrip("\n")
    else:
        body = args.body
    if not body:
        raise SystemExit("error: body is empty")

    host = services.resolve_host(getattr(args, "host", None))
    self_project, self_name = services.parse_self(
        args.self_arg,
        os.getcwd(),
    )
    peer_project, peer_name = services.resolve_peer(
        host,
        args.peer,
        require_full_session=True,
    )
    payload = {
        "self_project": self_project,
        "self": self_name,
        "peer_project": peer_project,
        "peer": peer_name,
        "body": body,
        "kind": args.kind,
    }
    if args.ask:
        payload["ask"] = args.ask
    response = services.request(
        "POST",
        host,
        "/send",
        body=payload,
    )
    if response.get("turn") is None:
        print(
            f"sent: msg_id={response['msg_id']} "
            f"broadcast->{peer_name}@{peer_project}"
        )
    else:
        print(
            f"sent: msg_id={response['msg_id']} "
            f"turn->{response['turn']}"
        )
    services.record_event("send")


def read(args, services: ConversationCommandServices) -> None:
    (
        host,
        self_project,
        self_name,
        peer_project,
        peer_name,
    ) = _participants(args, services)
    rows = services.request(
        "GET",
        host,
        "/read",
        params={
            "self_project": self_project,
            "self": self_name,
            "peer_project": peer_project,
            "peer": peer_name,
            "limit": args.limit,
            "since": args.since,
        },
    )
    for row in rows:
        ask = (row.get("ask") or "").replace("\n", " ")
        body = row["body"].replace("\n", "\\n")
        sender_project = row["sender_project"]
        sender_identity = (
            row["sender"]
            if sender_project == "*"
            else f"{row['sender']}@{sender_project}"
        )
        print(
            f"{row['id']}\t{row['created_at']}\t{sender_identity}\t"
            f"{row['kind']}\t{ask}\t{body}"
        )


def message(args, services: ConversationCommandServices) -> None:
    host = services.resolve_host(getattr(args, "host", None))
    self_project, self_name = services.parse_self(
        args.self_arg,
        os.getcwd(),
    )
    response = services.request(
        "GET",
        host,
        "/message",
        params={
            "project": self_project,
            "name": self_name,
            "id": args.message_id,
        },
    )
    if response.get("error"):
        raise SystemExit(f"message: {response['error']}")
    print(f"# Message {response['id']}")
    print(f"from: {response['sender_identity']}")
    print(f"to: {response['recipient_identity']}")
    print(f"kind: {response['kind']}")
    print(f"created_at: {response['created_at']}")
    if response.get("ask"):
        print(f"ask: {response['ask']}")
    print()
    print(response["body"])


def show(args, services: ConversationCommandServices) -> None:
    (
        host,
        self_project,
        self_name,
        peer_project,
        peer_name,
    ) = _participants(args, services)
    text = services.request(
        "GET",
        host,
        "/show",
        params={
            "self_project": self_project,
            "self": self_name,
            "peer_project": peer_project,
            "peer": peer_name,
            "limit": args.limit,
        },
    )
    sys.stdout.write(text)


def turn(args, services: ConversationCommandServices) -> None:
    (
        host,
        self_project,
        self_name,
        peer_project,
        peer_name,
    ) = _participants(args, services)
    response = services.request(
        "GET",
        host,
        "/turn",
        params={
            "self_project": self_project,
            "self": self_name,
            "peer_project": peer_project,
            "peer": peer_name,
        },
    )
    print(response["turn"])


def delete(args, services: ConversationCommandServices) -> None:
    (
        host,
        self_project,
        self_name,
        peer_project,
        peer_name,
    ) = _participants(args, services)
    response = services.request(
        "DELETE",
        host,
        "/conversation",
        params={
            "self_project": self_project,
            "self": self_name,
            "peer_project": peer_project,
            "peer": peer_name,
        },
    )
    print(
        f"deleted: {self_name}@{self_project}<->"
        f"{peer_name}@{peer_project} "
        f"({response['msg_count']} msgs purged)"
    )
