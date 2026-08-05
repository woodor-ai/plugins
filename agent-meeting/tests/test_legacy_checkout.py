from pathlib import Path
import stat

import pytest


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def test_remove_legacy_checkout_deletes_owned_tree(tmp_path):
    from agent_meeting.installation import legacy_checkout

    checkout = legacy_checkout.legacy_checkout(tmp_path)
    object_file = checkout / ".git" / "objects" / "01" / "object"
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(b"git object")

    assert legacy_checkout.remove_legacy_checkout(tmp_path) is True
    assert not checkout.exists()
    assert not checkout.parent.exists()


def test_readonly_windows_file_is_made_writable_and_retried(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.installation import legacy_checkout

    target = tmp_path / "object"
    chmod_calls = []
    remove_calls = []
    monkeypatch.setattr(
        legacy_checkout.os,
        "chmod",
        lambda path, mode: chmod_calls.append((Path(path), mode)),
    )
    monkeypatch.setattr(legacy_checkout.sys, "platform", "win32")

    error = PermissionError(5, "Access is denied", str(target))
    legacy_checkout._retry_readonly_removal(
        lambda path: remove_calls.append(Path(path)),
        str(target),
        (PermissionError, error, None),
    )

    assert chmod_calls == [(target, stat.S_IREAD | stat.S_IWRITE)]
    assert remove_calls == [target]


def test_non_permission_removal_error_is_not_retried(tmp_path, monkeypatch):
    from agent_meeting.installation import legacy_checkout

    monkeypatch.setattr(
        legacy_checkout.os,
        "chmod",
        lambda *_args: pytest.fail("non-permission error changed attributes"),
    )
    error = OSError("device error")

    with pytest.raises(OSError, match="device error"):
        legacy_checkout._retry_readonly_removal(
            lambda _path: pytest.fail("non-permission removal was retried"),
            str(tmp_path / "object"),
            (OSError, error, None),
        )


def test_cleanup_error_can_preserve_an_existing_install_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.installation import legacy_checkout

    checkout = legacy_checkout.legacy_checkout(tmp_path)
    checkout.mkdir(parents=True)
    monkeypatch.setattr(
        legacy_checkout.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError(5, "Access is denied", str(checkout))
        ),
    )

    assert legacy_checkout.remove_legacy_checkout(
        tmp_path,
        suppress_errors=True,
    ) is False
    assert "WARNING: could not remove legacy update checkout" in (
        capsys.readouterr().err
    )
