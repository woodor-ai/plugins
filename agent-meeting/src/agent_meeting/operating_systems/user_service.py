"""Cross-platform per-user service installation and lifecycle control."""

from __future__ import annotations

import codecs
import ctypes
import os
import platform
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


# Windows returns ERROR_CANCELLED when a user dismisses the UAC consent dialog.
ELEVATION_DECLINED_EXIT_CODE = 1223
ELEVATION_DISABLED_ENVIRONMENT_VARIABLE = "AGENT_MEETING_NO_ELEVATION"


@dataclass(frozen=True)
class UserServiceSpec:
    description: str
    command: tuple[str, ...]
    macos_label: str
    windows_task_name: str
    linux_unit_name: str
    log_path: Path
    process_type: str = "Background"
    environment: tuple[tuple[str, str], ...] = ()


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def _message(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip()


def _home(home: Path | None) -> Path:
    return Path.home() if home is None else home


def macos_plist_path(
    spec: UserServiceSpec,
    *,
    home: Path | None = None,
) -> Path:
    return _home(home) / "Library" / "LaunchAgents" / f"{spec.macos_label}.plist"


def macos_definition(spec: UserServiceSpec) -> bytes:
    payload = {
        "Label": spec.macos_label,
        "ProgramArguments": list(spec.command),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": spec.process_type,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    }
    if spec.environment:
        payload["EnvironmentVariables"] = dict(spec.environment)
    return plistlib.dumps(payload)


def linux_unit_path(
    spec: UserServiceSpec,
    *,
    home: Path | None = None,
) -> Path:
    return _home(home) / ".config" / "systemd" / "user" / spec.linux_unit_name


def linux_definition(spec: UserServiceSpec) -> str:
    command = " ".join(shlex.quote(part) for part in spec.command)
    environment = "".join(
        f'Environment="{key}={value}"\n'
        for key, value in spec.environment
    )
    return (
        "[Unit]\n"
        f"Description={spec.description}\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        f"{environment}"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_principal() -> str:
    """Return the security principal that owns and triggers the task.

    ``schtasks /SC ONLOGON`` registers a logon trigger for *any* user, which
    Windows only allows an elevated caller to do. Naming the current account
    keeps registration inside what an ordinary interactive user may do, so a
    normal ``am-update`` never needs administrator approval.
    """
    sid = _windows_user_sid()
    if sid:
        return sid
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def _windows_user_sid() -> str | None:
    token_query = 0x0008
    token_user = 1
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        ctypes.c_void_p(kernel32.GetCurrentProcess()),
        token_query,
        ctypes.byref(token),
    ):
        return None
    try:
        size = ctypes.c_ulong(0)
        advapi32.GetTokenInformation(
            token,
            token_user,
            None,
            0,
            ctypes.byref(size),
        )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user,
            buffer,
            size,
            ctypes.byref(size),
        ):
            return None
        # TOKEN_USER begins with a SID_AND_ATTRIBUTES whose first member is the
        # SID pointer.
        sid = ctypes.cast(
            buffer,
            ctypes.POINTER(ctypes.c_void_p),
        ).contents
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def windows_definition(
    spec: UserServiceSpec,
    *,
    principal: str | None = None,
) -> str:
    """Render the Task Scheduler XML registered for ``spec``.

    ``spec.environment`` has no Task Scheduler equivalent; the Windows service
    entrypoints take their configuration from explicit command arguments.
    """
    principal = windows_principal() if principal is None else principal
    executable, *arguments = spec.command
    owner = escape(principal)
    argument_line = (
        f"      <Arguments>"
        f"{escape(subprocess.list2cmdline(arguments))}"
        f"</Arguments>\n"
        if arguments
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/'
        '2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{escape(spec.description)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{owner}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{owner}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false"
        "</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        # A long-lived daemon must never be terminated on a schedule.
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>3</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(executable)}</Command>\n"
        f"{argument_line}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def windows_task_command(
    spec: UserServiceSpec,
    definition_path: Path,
) -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/TN",
        spec.windows_task_name,
        "/XML",
        str(definition_path),
        "/F",
    ]


def elevation_allowed() -> bool:
    disabled = os.environ.get(
        ELEVATION_DISABLED_ENVIRONMENT_VARIABLE,
        "",
    ).strip().lower()
    return disabled not in ("1", "true", "yes", "on")


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _elevated_script(commands: list[list[str]], log_path: Path) -> str:
    def quote(value: str) -> str:
        escaped = value.replace("'", "''")
        return f"'{escaped}'"

    lines = ["$ErrorActionPreference = 'Continue'"]
    for command in commands:
        executable, *arguments = command
        rendered = " ".join(quote(argument) for argument in arguments)
        lines.append(
            f"& {quote(executable)} {rendered} *>> {quote(str(log_path))}"
        )
    # Only the final command decides the outcome; the ones before it stop a
    # running instance and are allowed to fail.
    lines.append("exit $LASTEXITCODE")
    return "\n".join(lines) + "\n"


def _run_elevated(commands: list[list[str]]) -> subprocess.CompletedProcess:
    """Run ``commands`` with administrator rights via a UAC consent prompt.

    The whole sequence runs inside one elevated shell so the user is asked to
    approve once, not once per command.
    """
    if _is_elevated():
        result = None
        for command in commands:
            result = _run(command)
        return result or subprocess.CompletedProcess(commands, 0, "", "")
    directory = Path(tempfile.mkdtemp(prefix="agent-meeting-elevate-"))
    script = directory / "run.ps1"
    log_path = directory / "output.log"
    script.write_text(
        _elevated_script(commands, log_path),
        encoding="utf-8-sig",
    )
    launcher = subprocess.list2cmdline(
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    ).replace("'", "''")
    try:
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "try { $process = Start-Process -FilePath 'powershell.exe' "
                f"-ArgumentList '{launcher}' "
                "-Verb RunAs -WindowStyle Hidden -PassThru -Wait } "
                f"catch {{ exit {ELEVATION_DECLINED_EXIT_CODE} }}; "
                "exit $process.ExitCode",
            ]
        )
    except OSError as error:
        _remove_tree(directory)
        return subprocess.CompletedProcess(commands, 1, "", str(error))
    try:
        output = _readable(_decode(log_path.read_bytes()))
    except OSError:
        output = ""
    finally:
        _remove_tree(directory)
    if result.returncode == ELEVATION_DECLINED_EXIT_CODE:
        output = output or "administrator approval was declined"
    return subprocess.CompletedProcess(
        commands,
        result.returncode,
        output,
        _message(result),
    )


def _remove_tree(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


def _decode(data: bytes) -> str:
    """Decode a transcript written by whichever PowerShell edition ran it.

    Windows PowerShell redirects to UTF-16; PowerShell 7 redirects to UTF-8.
    """
    for prefix, encoding in (
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    ):
        if data.startswith(prefix):
            return data.decode(encoding, errors="replace")
    return data.decode("utf-8", errors="replace")


def _readable(text: str) -> str:
    """Drop the frame PowerShell wraps around a native command's stderr."""
    noise = ("+", "At ", "CategoryInfo", "FullyQualifiedErrorId")
    lines = [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith(noise)
    ]
    return "\n".join(lines)


def _announce_elevation(reason: str) -> None:
    print(
        f"agent-meeting: {reason}\n"
        "agent-meeting: requesting administrator approval "
        "(a Windows UAC prompt will appear)",
        file=sys.stderr,
        flush=True,
    )


def _windows_task_exists(task_name: str) -> bool:
    return _run(["schtasks", "/Query", "/TN", task_name]).returncode == 0


def _refusal(spec: UserServiceSpec, action: str) -> str:
    return f"scheduled task {spec.windows_task_name} cannot be {action}"


def _may_elevate(task_name: str) -> bool:
    """Only a registered task can be blocked by a privilege we can raise."""
    return (
        not _is_elevated()
        and elevation_allowed()
        and _windows_task_exists(task_name)
    )


def _run_windows_task(
    command: list[str],
    *,
    task_name: str,
    reason: str,
) -> subprocess.CompletedProcess:
    """Run a task command, retrying once with elevation when it is refused."""
    result = _run(command)
    if result.returncode == 0 or not _may_elevate(task_name):
        return result
    _announce_elevation(reason)
    return _run_elevated([command])


def _register_windows_task(spec: UserServiceSpec) -> None:
    directory = Path(tempfile.mkdtemp(prefix="agent-meeting-task-"))
    definition = directory / f"{spec.windows_task_name}.xml"
    # schtasks only accepts UTF-16 task definitions.
    definition.write_text(windows_definition(spec), encoding="utf-16")
    create = windows_task_command(spec, definition)
    try:
        result = _run(create)
        if result.returncode == 0:
            return
        if not _may_elevate(spec.windows_task_name):
            raise RuntimeError(_registration_failure(spec, _message(result)))
        _announce_elevation(
            f"scheduled task {spec.windows_task_name} cannot be updated by "
            "the current user, because an elevated install registered it"
        )
        # Re-registering it while elevated would leave the same restriction
        # behind. Remove it once with approval instead, then register it again
        # as this user so later updates never prompt.
        removed = _run_elevated(
            [
                ["schtasks", "/End", "/TN", spec.windows_task_name],
                ["schtasks", "/Delete", "/TN", spec.windows_task_name, "/F"],
            ]
        )
        if removed.returncode == ELEVATION_DECLINED_EXIT_CODE:
            raise RuntimeError(
                _registration_failure(
                    spec,
                    _message(result),
                    _message(removed),
                )
            )
        if removed.returncode == 0:
            retry = _run(create)
            if retry.returncode == 0:
                return
            result = retry
        elevated = _run_elevated([create])
        if elevated.returncode != 0:
            raise RuntimeError(
                _registration_failure(
                    spec,
                    _message(result),
                    _message(elevated),
                )
            )
    finally:
        _remove_tree(directory)


def _registration_failure(spec: UserServiceSpec, *errors: str) -> str:
    detail = "; ".join(error for error in errors if error)
    return (
        f"could not register scheduled task {spec.windows_task_name}: "
        f"{detail or 'schtasks failed'}"
    )


def ensure_installed(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> bool:
    system_name = system_name or platform.system()
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = macos_definition(spec)
        changed = not path.exists() or path.read_bytes() != expected
        if changed:
            temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
            temporary.write_bytes(expected)
            os.replace(temporary, path)
        return changed
    if system_name == "Linux":
        path = linux_unit_path(spec, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = linux_definition(spec)
        changed = (
            not path.exists()
            or path.read_text(encoding="utf-8") != expected
        )
        if changed:
            temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
            temporary.write_text(expected, encoding="utf-8")
            os.replace(temporary, path)
            result = _run(["systemctl", "--user", "daemon-reload"])
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or "systemctl daemon-reload failed"
                )
        return changed
    if system_name == "Windows":
        _register_windows_task(spec)
        return True
    raise RuntimeError(f"unsupported user-service platform: {system_name}")


def _macos_bootstrap(domain: str, path: Path) -> subprocess.CompletedProcess:
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    for _attempt in range(2):
        if result.returncode == 0:
            break
        time.sleep(0.1)
        result = _run(["launchctl", "bootstrap", domain, str(path)])
    return result


def is_installed(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> bool:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return False
        return payload.get("ProgramArguments") == list(spec.command)
    if system_name == "Linux":
        path = linux_unit_path(spec, home=home)
        return (
            path.exists()
            and path.read_text(encoding="utf-8") == linux_definition(spec)
        )
    if system_name == "Windows":
        return _run(
            ["schtasks", "/Query", "/TN", spec.windows_task_name]
        ).returncode == 0
    return False


def start(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> None:
    system_name = system_name or platform.system()
    definition_changed = ensure_installed(
        spec,
        system_name=system_name,
        home=home,
    )
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{spec.macos_label}"
        enabled = _run(["launchctl", "enable", target])
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "launchctl enable failed"
            )
        listed = _run(["launchctl", "print", target])
        if listed.returncode == 0 and definition_changed:
            _run(["launchctl", "bootout", target])
            result = _macos_bootstrap(domain, path)
        elif listed.returncode == 0:
            result = _run(["launchctl", "kickstart", "-k", target])
        else:
            result = _macos_bootstrap(domain, path)
    elif system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "enable", "--now", spec.linux_unit_name]
        )
    else:
        enabled = _run_windows_task(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Enable"],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "enabled"),
        )
        if enabled.returncode != 0:
            raise RuntimeError(_message(enabled))
        result = _run_windows_task(
            ["schtasks", "/Run", "/TN", spec.windows_task_name],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "started"),
        )
    if result.returncode != 0:
        raise RuntimeError(_message(result))


def stop(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        target = f"gui/{os.getuid()}/{spec.macos_label}"
        result = _run(["launchctl", "bootout", target])
        if (
            result.returncode not in (0, 3)
            and "Could not find service" not in result.stderr
        ):
            raise RuntimeError(result.stderr.strip() or "launchctl stop failed")
        disabled = _run(["launchctl", "disable", target])
        if disabled.returncode != 0:
            raise RuntimeError(
                disabled.stderr.strip() or "launchctl disable failed"
            )
        return
    if system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "disable", "--now", spec.linux_unit_name]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl stop failed")
        return
    if system_name == "Windows":
        _run(["schtasks", "/End", "/TN", spec.windows_task_name])
        result = _run_windows_task(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Disable"],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "stopped"),
        )
        if result.returncode != 0:
            raise RuntimeError(_message(result))
        return
    raise RuntimeError(f"unsupported user-service platform: {system_name}")


def uninstall(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> None:
    """Stop and remove a per-user service definition when present."""
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        target = f"gui/{os.getuid()}/{spec.macos_label}"
        result = _run(["launchctl", "bootout", target])
        if (
            result.returncode not in (0, 3)
            and "Could not find service" not in result.stderr
        ):
            raise RuntimeError(
                result.stderr.strip() or "launchctl bootout failed"
            )
        _run(["launchctl", "enable", target])
        path = macos_plist_path(spec, home=home)
        path.unlink(missing_ok=True)
        return
    if system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "disable", "--now", spec.linux_unit_name]
        )
        if result.returncode != 0 and is_installed(
            spec,
            system_name=system_name,
            home=home,
        ):
            raise RuntimeError(
                result.stderr.strip() or "systemctl disable failed"
            )
        linux_unit_path(spec, home=home).unlink(missing_ok=True)
        reloaded = _run(["systemctl", "--user", "daemon-reload"])
        if reloaded.returncode != 0:
            raise RuntimeError(
                reloaded.stderr.strip() or "systemctl daemon-reload failed"
            )
        return
    if system_name == "Windows":
        _run(["schtasks", "/End", "/TN", spec.windows_task_name])
        result = _run_windows_task(
            ["schtasks", "/Delete", "/TN", spec.windows_task_name, "/F"],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "removed"),
        )
        if result.returncode != 0 and is_installed(
            spec,
            system_name=system_name,
            home=home,
        ):
            raise RuntimeError(_message(result))
        return
    raise RuntimeError(f"unsupported user-service platform: {system_name}")


def restart(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> None:
    system_name = system_name or platform.system()
    definition_changed = ensure_installed(
        spec,
        system_name=system_name,
        home=home,
    )
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{spec.macos_label}"
        enabled = _run(["launchctl", "enable", target])
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "launchctl enable failed"
            )
        listed = _run(["launchctl", "print", target])
        if listed.returncode == 0 and definition_changed:
            _run(["launchctl", "bootout", target])
            result = _macos_bootstrap(domain, path)
        elif listed.returncode == 0:
            result = _run(["launchctl", "kickstart", "-k", target])
        else:
            result = _macos_bootstrap(domain, path)
    elif system_name == "Linux":
        enabled = _run(
            ["systemctl", "--user", "enable", spec.linux_unit_name]
        )
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "systemctl enable failed"
            )
        result = _run(["systemctl", "--user", "restart", spec.linux_unit_name])
    else:
        _run(["schtasks", "/End", "/TN", spec.windows_task_name])
        enabled = _run_windows_task(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Enable"],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "enabled"),
        )
        if enabled.returncode != 0:
            raise RuntimeError(_message(enabled))
        result = _run_windows_task(
            ["schtasks", "/Run", "/TN", spec.windows_task_name],
            task_name=spec.windows_task_name,
            reason=_refusal(spec, "started"),
        )
    if result.returncode != 0:
        raise RuntimeError(_message(result))


def state(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
) -> str:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        target = f"gui/{os.getuid()}/{spec.macos_label}"
        registered = _run(["launchctl", "print", target]).returncode == 0
        return "registered" if registered else "not-registered"
    if system_name == "Linux":
        result = _run(["systemctl", "--user", "is-active", spec.linux_unit_name])
        return result.stdout.strip() or "inactive"
    if system_name == "Windows":
        result = _run(["schtasks", "/Query", "/TN", spec.windows_task_name])
        return "registered" if result.returncode == 0 else "not-registered"
    return "unsupported"
