from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from openconstraint_mcp.runtime_install.core import (
    MANAGED_RUNTIME_MARKER,
    _write_runtime_marker,
    install_managed_runtime,
)
from openconstraint_mcp.runtime_install.download import MINIZINC_VERSION
from openconstraint_mcp.runtime_install.errors import RuntimeInstallError


@pytest.fixture
def stub_linux_x86_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.sys.platform", "linux")
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.core.platform.machine", lambda: "x86_64"
    )


@pytest.fixture
def stub_download_with_fixture(
    fake_minizinc_tarball: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    """Replace ``_download_archive`` with a copy of the fake fixture. Returns a
    list of destination paths so tests can assert how many times it was called
    (or that it was not called at all)."""
    calls: list[Path] = []

    def _fake_download(url: str, dest: Path, expected_sha256: str, console: Console) -> None:
        calls.append(dest)
        shutil.copy(fake_minizinc_tarball, dest)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core._download_archive", _fake_download)
    return calls


def _quiet_console() -> Console:
    return Console(quiet=True)


def test_install_happy_path_into_missing_target(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    result = install_managed_runtime(target, console=_quiet_console())
    assert result == target.resolve()
    binary = result / "bin" / "minizinc"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)


def test_install_writes_marker(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    result = install_managed_runtime(target, console=_quiet_console())
    marker = result / MANAGED_RUNTIME_MARKER
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["managed_by"] == "openconstraint-mcp"
    assert data["minizinc_version"] == MINIZINC_VERSION


def test_install_rejects_unsupported_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.sys.platform", "darwin")
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.core.platform.machine", lambda: "x86_64"
    )
    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(tmp_path / "runtime", console=_quiet_console())
    assert "Linux x86_64" in str(exc_info.value)


def test_install_rejects_unsupported_arch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.core.sys.platform", "linux")
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.core.platform.machine", lambda: "aarch64"
    )
    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(tmp_path / "runtime", console=_quiet_console())
    assert "Linux x86_64" in str(exc_info.value)


def test_install_refuses_unmanaged_nonempty_target_even_with_yes(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "unrelated.txt").write_text("important user data")

    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(target, yes=True, console=_quiet_console())
    message = str(exc_info.value)
    assert str(target.resolve()) in message
    assert (target / "unrelated.txt").read_text() == "important user data"
    assert stub_download_with_fixture == []


def test_install_managed_nonempty_target_without_yes_refused(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)

    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(target, yes=False, console=_quiet_console())
    assert "refusing to overwrite non-empty runtime directory" in str(exc_info.value)
    assert (target / MANAGED_RUNTIME_MARKER).is_file()


def test_install_managed_nonempty_target_with_yes_replaces(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)
    (target / "old-evidence").write_text("from a prior install")

    install_managed_runtime(target, yes=True, console=_quiet_console())

    # The swap replaced the directory wholesale: the old-evidence file is gone,
    # a fresh marker is present, and the new binary is there.
    assert not (target / "old-evidence").exists()
    assert (target / MANAGED_RUNTIME_MARKER).is_file()
    assert (target / "bin" / "minizinc").is_file()


def test_install_refuses_when_target_is_a_regular_file(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime-file"
    target.write_text("not a directory")

    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(target, yes=True, console=_quiet_console())
    assert "not a directory" in str(exc_info.value).lower()
    assert stub_download_with_fixture == []


def test_install_refuses_with_stale_backup_when_runtime_missing(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    stale_backup = tmp_path / f".{target.name}.backup.99999"
    stale_backup.mkdir()
    (stale_backup / "marker").write_text("would-be prior runtime")

    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(target, yes=True, console=_quiet_console())
    message = str(exc_info.value)
    assert str(stale_backup) in message
    assert f"mv {stale_backup} {target.resolve()}" in message
    assert "rm -rf" not in message
    assert stub_download_with_fixture == []
    assert stale_backup.is_dir()


def test_install_refuses_with_stale_backup_when_runtime_present(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)

    stale_backup = tmp_path / f".{target.name}.backup.99999"
    stale_backup.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(target, yes=True, console=_quiet_console())
    message = str(exc_info.value)
    assert str(stale_backup) in message
    assert f"rm -rf {stale_backup}" in message
    assert " mv " not in message
    assert stub_download_with_fixture == []
    assert stale_backup.is_dir()
    assert (target / MANAGED_RUNTIME_MARKER).is_file()


def test_install_smoke_check_failure_leaves_target_untouched(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(binary: Path) -> None:
        raise RuntimeInstallError("smoke-check failed")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.core._smoke_check_binary", _boom)

    target = tmp_path / "runtime"
    with pytest.raises(RuntimeInstallError):
        install_managed_runtime(target, console=_quiet_console())
    assert not (target / "bin" / "minizinc").exists()
    # Staging siblings must not be left behind.
    siblings = [p for p in tmp_path.iterdir() if p.name.startswith(f".{target.name}.")]
    assert siblings == []


def test_install_rename_failure_restores_prior_runtime(
    tmp_path: Path,
    stub_linux_x86_64: None,
    stub_download_with_fixture: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    _write_runtime_marker(target)
    (target / "prior-evidence").write_text("prior install")

    real_rename = Path.rename
    call_counter = {"n": 0}

    def _flaky_rename(self: Path, target_path: Path | str) -> Path:
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            raise OSError("simulated rename failure")
        return real_rename(self, target_path)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(OSError):
        install_managed_runtime(target, yes=True, console=_quiet_console())

    # Prior runtime is intact …
    assert (target / MANAGED_RUNTIME_MARKER).is_file()
    assert (target / "prior-evidence").read_text() == "prior install"
    # … and no staging/backup siblings remain.
    siblings = [p for p in tmp_path.iterdir() if p.name.startswith(f".{target.name}.")]
    assert siblings == []
