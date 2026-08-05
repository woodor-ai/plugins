"""Safe, manifest-driven complete uninstall for agent-meeting."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from agent_meeting.installation import install_manifest, uninstall_cleanup
from agent_meeting.lifecycle_control import user_service as lifecycle_user_service
from agent_meeting.operating_systems import message_hub_user_service


PLUGIN_ID = "agent-meeting@woodor"


def _codex_daemon_info() -> dict:
    try:
        from mycodex.commands import am_codexd_cli
    except ImportError:
        return {}
    return am_codexd_cli.status_info()


def _stop_codex_daemon() -> None:
    try:
        from mycodex.commands import am_codexd_cli
    except ImportError:
        return
    am_codexd_cli.stop()


def _remove_path_entry(meeting_home: Path) -> None:
    if sys.platform.startswith("win"):
        from mycodex.operating_systems.windows import user_command_path

        user_command_path.remove_command_directory(meeting_home / "bin")
    else:
        from mycodex.operating_systems.macos import shell_command_path

        shell_command_path.remove_command_directory(meeting_home / "bin")


def _json_command(command: list[str]) -> object | None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _plugin_is_installed(client: str, executable: str) -> bool | None:
    payload = _json_command([executable, "plugin", "list", "--json"])
    if client == "codex" and isinstance(payload, dict):
        plugins = payload.get("installed") or payload.get("plugins") or []
        return any(
            item.get("pluginId") == PLUGIN_ID and item.get("installed")
            for item in plugins
        )
    if client == "claude-code" and isinstance(payload, list):
        return any(item.get("id") == PLUGIN_ID for item in payload)
    return None


def _remove_plugin(client: str) -> None:
    executable_name = "codex" if client == "codex" else "claude"
    executable = shutil.which(executable_name)
    if not executable:
        print(
            f"warning: {executable_name} CLI not found; "
            f"skipping its already-inaccessible plugin registration",
            file=sys.stderr,
        )
        return
    installed = _plugin_is_installed(client, executable)
    if installed is False:
        return
    command = (
        [executable, "plugin", "remove", PLUGIN_ID]
        if client == "codex"
        else [executable, "plugin", "uninstall", PLUGIN_ID, "-y"]
    )
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(
            f"{executable_name} could not remove {PLUGIN_ID}; "
            "the runtime was preserved so the uninstall can be retried"
        )


def _print_plan(meeting_home: Path, targets: list[str]) -> None:
    print("agent-meeting complete uninstall plan:")
    for target in targets:
        print(f"  - remove {target} plugin registration: {PLUGIN_ID}")
    print("  - stop and delete am-msgd and am-ctld user services")
    if "codex" in targets:
        print("  - stop am-codexd (only when no sessions are active)")
    print(f"  - remove the PATH entry: {meeting_home / 'bin'}")
    print(f"  - permanently delete runtime, config, logs, and messages: {meeting_home}")
    print("  - preserve the shared woodor marketplace registration")


def run(
    meeting_home: Path,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> int:
    meeting_home = meeting_home.resolve()
    manifest = install_manifest.read_manifest(meeting_home)
    targets = list(manifest["targets"])
    _print_plan(meeting_home, targets)
    if dry_run:
        print("dry run: nothing was changed")
        return 0

    if "codex" in targets:
        daemon = _codex_daemon_info()
        sessions = int(daemon.get("sessions") or 0)
        if sessions:
            raise RuntimeError(
                f"cannot uninstall while {sessions} amcodex session(s) are active"
            )

    if not assume_yes:
        confirmation = input("Type uninstall to permanently delete these files: ")
        if confirmation.strip() != "uninstall":
            print("uninstall cancelled")
            return 1

    if "codex" in targets:
        _stop_codex_daemon()
    message_hub_user_service.uninstall(
        meeting_home,
        system_name=platform.system(),
    )
    lifecycle_user_service.uninstall_lifecycle_control_service(
        meeting_home,
        system_name=platform.system(),
    )
    for target in targets:
        _remove_plugin(target)
    _remove_path_entry(meeting_home)
    uninstall_cleanup.schedule_cleanup(meeting_home)
    print(
        "agent-meeting uninstall scheduled; its installation directory "
        "will disappear after this command exits"
    )
    return 0
