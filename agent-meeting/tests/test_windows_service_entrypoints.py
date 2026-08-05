from __future__ import annotations

from pathlib import Path


def test_service_entrypoint_redirects_output_and_forwards_arguments(tmp_path):
    from agent_meeting.operating_systems.windows_service_entrypoints import (
        _run_service_entrypoint,
    )

    log_path = tmp_path / "logs" / "service.log"
    received = []

    def service_main(argv):
        received.append(argv)
        print("standard output")
        import sys

        print("standard error", file=sys.stderr)
        return 0

    result = _run_service_entrypoint(
        service_main,
        [
            "--service-log",
            str(log_path),
            "serve",
            "--config",
            "config.json",
        ],
    )

    assert result == 0
    assert received == [["serve", "--config", "config.json"]]
    assert log_path.read_text(encoding="utf-8") == (
        "standard output\nstandard error\n"
    )


def test_service_entrypoint_logs_unhandled_exceptions(tmp_path):
    from agent_meeting.operating_systems.windows_service_entrypoints import (
        _run_service_entrypoint,
    )

    log_path = tmp_path / "service.log"

    def fail(_argv):
        raise RuntimeError("service failed")

    assert _run_service_entrypoint(
        fail,
        ["--service-log", str(log_path)],
    ) == 1
    assert "RuntimeError: service failed" in log_path.read_text(
        encoding="utf-8"
    )


def test_service_entrypoint_requires_log_path():
    from agent_meeting.operating_systems.windows_service_entrypoints import (
        _run_service_entrypoint,
    )

    called = False

    def service_main(_argv):
        nonlocal called
        called = True
        return 0

    assert _run_service_entrypoint(service_main, []) == 2
    assert called is False


def test_gui_entrypoints_are_declared_in_package_manifest():
    import tomllib

    manifest = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["project"]["gui-scripts"] == {
        "am-ctld-service": (
            "agent_meeting.operating_systems.windows_service_entrypoints:"
            "am_ctld_service_main"
        ),
        "am-msgd-service": (
            "agent_meeting.operating_systems.windows_service_entrypoints:"
            "am_msgd_service_main"
        ),
    }
