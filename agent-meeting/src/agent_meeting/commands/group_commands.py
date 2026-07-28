"""Group-management commands for the public meeting client."""

from __future__ import annotations

import os
import re
import sys
from typing import Callable


def validate_group_name(name: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9-]{2,20}", name) or "--" in name:
        return (
            f"invalid group name '{name}': must be 2-20 chars, "
            "only [A-Za-z0-9-], no '--'"
        )
    return None


def _identity_parts(raw: str, default_project: str) -> tuple[str, str]:
    if "@" in raw:
        name, _, project = raw.partition("@")
        return name, project
    return raw, default_project


def _exit_on_error(response) -> None:
    if isinstance(response, dict) and response.get("error"):
        print(response["error"], file=sys.stderr)
        raise SystemExit(1)


def run_group_command(
    args,
    *,
    resolve_host: Callable[[str | None], str],
    derive_project: Callable[[str], str],
    request: Callable,
) -> None:
    host = resolve_host(getattr(args, "host", None))
    project = derive_project(os.getcwd())

    if args.group_cmd == "create":
        group_name, group_project = _identity_parts(
            args.group_name,
            project,
        )
        error = validate_group_name(group_name)
        if error:
            print(error, file=sys.stderr)
            raise SystemExit(1)
        members = [
            member.strip()
            for member in args.members.split(",")
            if member.strip()
        ]
        response = request(
            "POST",
            host,
            "/group/create",
            body={
                "project": group_project,
                "name": group_name,
                "members": members,
                "creator": getattr(args, "creator", None),
            },
        )
        _exit_on_error(response)
        print(
            f"group created: {group_name}@{group_project} "
            f"members={response.get('members', members)}"
        )
        return

    if args.group_cmd in ("add", "remove"):
        group_name, group_project = _identity_parts(
            args.group_name,
            project,
        )
        if args.group_cmd == "add":
            error = validate_group_name(group_name)
            if error:
                print(error, file=sys.stderr)
                raise SystemExit(1)
        member_name, member_project = _identity_parts(
            args.member,
            project,
        )
        response = request(
            "POST",
            host,
            f"/group/{args.group_cmd}",
            body={
                "group_project": group_project,
                "group": group_name,
                "member_project": member_project,
                "member": member_name,
            },
        )
        _exit_on_error(response)
        verb = "added" if args.group_cmd == "add" else "removed"
        direction = "to" if args.group_cmd == "add" else "from"
        print(
            f"{verb} {member_name}@{member_project} {direction} "
            f"{group_name}@{group_project}"
        )
        return

    if args.group_cmd == "rename":
        old_name, group_project = _identity_parts(
            args.old_name,
            project,
        )
        response = request(
            "POST",
            host,
            "/group/rename",
            body={
                "project": group_project,
                "old": old_name,
                "new": args.new_name,
            },
        )
        _exit_on_error(response)
        print(
            f"group renamed: {old_name} -> {args.new_name} "
            f"in project {group_project} "
            f"({response.get('messages_migrated', 0)} messages migrated)"
        )
        return

    if args.group_cmd == "list":
        params = {}
        if getattr(args, "member", None):
            member_name, member_project = _identity_parts(
                args.member,
                project,
            )
            params["member_project"] = member_project
            params["member"] = member_name
        response = request(
            "GET",
            host,
            "/group/list",
            params=params or None,
        )
        for group in response:
            print(
                f"{group['name']}@{group['project']}\t"
                f"{group.get('member_count', '')}"
            )
        return

    if args.group_cmd == "members":
        group_name, group_project = _identity_parts(
            args.group_name,
            project,
        )
        response = request(
            "GET",
            host,
            "/group/members",
            params={
                "group_project": group_project,
                "group": group_name,
            },
        )
        _exit_on_error(response)
        for member in response:
            print(member)
        return

    if args.group_cmd == "delete":
        group_name, group_project = _identity_parts(
            args.group_name,
            project,
        )
        response = request(
            "DELETE",
            host,
            "/group",
            params={
                "project": group_project,
                "name": group_name,
            },
        )
        _exit_on_error(response)
        print(
            f"group deleted: {group_name}@{group_project} "
            f"({response.get('purged', 0)} messages purged)"
        )
        return

    if args.group_cmd == "charter":
        group_name, group_project = _identity_parts(
            args.group_name,
            project,
        )
        if args.clear:
            response = request(
                "POST",
                host,
                "/group/charter",
                body={
                    "project": group_project,
                    "name": group_name,
                    "charter": None,
                },
            )
            _exit_on_error(response)
            print(f"charter cleared: {group_name}@{group_project}")
            return
        if args.charter_text:
            charter = " ".join(args.charter_text)
            response = request(
                "POST",
                host,
                "/group/charter",
                body={
                    "project": group_project,
                    "name": group_name,
                    "charter": charter,
                },
            )
            _exit_on_error(response)
            print(f"charter set: {group_name}@{group_project}")
            print(charter)
            return

        response = request(
            "GET",
            host,
            "/group/charter",
            params={
                "project": group_project,
                "name": group_name,
            },
        )
        _exit_on_error(response)
        charter = response.get("charter", "")
        print(
            charter
            or f"(no charter set for {group_name}@{group_project})"
        )
