"""Scope shared Codex app-server traffic to one mycodex session lease."""

from __future__ import annotations


def runtime_instructions(session) -> str:
    return (
        "Agent-meeting runtime for this Codex thread:\n"
        f"- Agent-meeting recipient: {session.identity}\n"
        f"- Agent-meeting control: {session.control_url}\n"
        "Pass these exact values as explicit am CLI arguments. Use the "
        "recipient as the positional <self> argument and pass "
        f"`--host {session.control_url}`. For `am group`, place the "
        "`--host` option immediately after `group`. Do not read "
        "MEETING_SELF or AM_MSGD_HOST from the environment."
    )


def scope_client_request(session, message):
    method = message.get("method")
    if method not in (
        "thread/start",
        "thread/resume",
        "thread/fork",
        "turn/start",
    ):
        return message

    scoped = dict(message)
    params = dict(message.get("params") or {})
    instructions = runtime_instructions(session)
    if method == "turn/start":
        additional_context = dict(params.get("additionalContext") or {})
        additional_context["agent-meeting-runtime"] = {
            "kind": "application",
            "value": instructions,
        }
        params["additionalContext"] = additional_context
    else:
        params["cwd"] = session.cwd
        existing = params.get("developerInstructions")
        if existing:
            instructions = f"{str(existing).rstrip()}\n\n{instructions}"
        params["developerInstructions"] = instructions
    scoped["params"] = params
    return scoped
