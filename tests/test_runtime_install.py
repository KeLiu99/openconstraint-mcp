from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from openconstraint_mcp import runtime_install
from openconstraint_mcp.runtime_install import (
    MANAGED_RUNTIME_MARKER,
    MINIZINC_VERSION,
    RuntimeInstallError,
    install_managed_runtime,
)


def _make_tar(path: Path, members: list[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info in members:
            if info.type == tarfile.SYMTYPE:
                tar.addfile(info)
            elif info.type == tarfile.DIRTYPE:
                tar.addfile(info)
            else:
                tar.addfile(info, io.BytesIO(b""))
    return path


def test_module_exposes_version_constant() -> None:
    assert MINIZINC_VERSION == "2.9.7"


def test_runtime_install_error_is_runtime_error() -> None:
    assert issubclass(RuntimeInstallError, RuntimeError)


def test_install_managed_runtime_is_callable() -> None:
    # Stub at Task 1 — orchestrator body is implemented in Task 5.
    assert callable(install_managed_runtime)
    assert hasattr(runtime_install, "install_managed_runtime")


def test_fake_minizinc_tarball_has_single_wrapper_dir(fake_minizinc_tarball: Path) -> None:
    with tarfile.open(fake_minizinc_tarball, "r:gz") as tar:
        names = tar.getnames()
    top_level = {name.split("/", 1)[0] for name in names}
    assert len(top_level) == 1, f"expected one top-level dir, got {top_level}"
    assert any(name.endswith("/bin/minizinc") for name in names)


# ---------------------------------------------------------------------------
# Task 3: _extract_bundle


def test_extract_bundle_strips_wrapper(
    fake_minizinc_tarball: Path,
    tmp_path: Path,
) -> None:
    dest = tmp_path / "runtime"
    dest.mkdir()
    runtime_install._extract_bundle(fake_minizinc_tarball, dest)

    binary = dest / "bin" / "minizinc"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    # Wrapper dir and the _extract scratch dir must both be gone.
    assert not (dest / "MiniZincIDE-2.9.7-bundle-linux-x86_64").exists()
    assert not (dest / "_extract").exists()


def test_extract_bundle_rejects_two_top_level_dirs(tmp_path: Path) -> None:
    archive = tmp_path / "twotops.tgz"

    def _dir(name: str) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        return info

    _make_tar(archive, [_dir("first"), _dir("second")])
    dest = tmp_path / "runtime"
    dest.mkdir()
    with pytest.raises(RuntimeInstallError):
        runtime_install._extract_bundle(archive, dest)


def test_extract_bundle_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tgz"

    wrapper = tarfile.TarInfo(name="wrapper")
    wrapper.type = tarfile.DIRTYPE
    wrapper.mode = 0o755

    escape = tarfile.TarInfo(name="wrapper/../../etc/evil")
    escape.size = 0
    escape.mode = 0o644

    _make_tar(archive, [wrapper, escape])
    dest = tmp_path / "runtime"
    dest.mkdir()
    with pytest.raises(RuntimeInstallError):
        runtime_install._extract_bundle(archive, dest)


def test_extract_bundle_rejects_absolute_path_member(tmp_path: Path) -> None:
    archive = tmp_path / "abspath.tgz"

    wrapper = tarfile.TarInfo(name="wrapper")
    wrapper.type = tarfile.DIRTYPE
    wrapper.mode = 0o755

    escape = tarfile.TarInfo(name="/etc/passwd")
    escape.size = 0
    escape.mode = 0o644

    _make_tar(archive, [wrapper, escape])
    dest = tmp_path / "runtime"
    dest.mkdir()
    with pytest.raises(RuntimeInstallError):
        runtime_install._extract_bundle(archive, dest)


def test_extract_bundle_rejects_symlink_with_absolute_target(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.tgz"

    wrapper = tarfile.TarInfo(name="wrapper")
    wrapper.type = tarfile.DIRTYPE
    wrapper.mode = 0o755

    link = tarfile.TarInfo(name="wrapper/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    link.mode = 0o777

    _make_tar(archive, [wrapper, link])
    dest = tmp_path / "runtime"
    dest.mkdir()
    with pytest.raises(RuntimeInstallError):
        runtime_install._extract_bundle(archive, dest)


# ---------------------------------------------------------------------------
# Task 4: _download_archive


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> None:
    """Patch httpx.Client so it routes traffic through ``handler``.

    Capture the real ``httpx.Client`` *before* patching so the factory below can
    construct an actual client without recursing into itself.
    """
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def _factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.httpx.Client", _factory)


def test_download_archive_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"fake bundle bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    runtime_install._download_archive(
        "https://example.invalid/bundle.tgz",
        dest,
        expected_sha256=digest,
        console=Console(quiet=True),
    )
    assert dest.read_bytes() == payload


def test_download_archive_sha256_mismatch_deletes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"tampered bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    with pytest.raises(RuntimeInstallError) as exc_info:
        runtime_install._download_archive(
            "https://example.invalid/bundle.tgz",
            dest,
            expected_sha256="0" * 64,
            console=Console(quiet=True),
        )
    assert "checksum" in str(exc_info.value).lower()
    assert not dest.exists()


def test_download_archive_http_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    with pytest.raises(RuntimeInstallError) as exc_info:
        runtime_install._download_archive(
            "https://example.invalid/bundle.tgz",
            dest,
            expected_sha256="0" * 64,
            console=Console(quiet=True),
        )
    assert "download" in str(exc_info.value).lower()
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Task 5: install_managed_runtime orchestrator


@pytest.fixture
def stub_linux_x86_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.sys.platform", "linux")
    monkeypatch.setattr("openconstraint_mcp.runtime_install.platform.machine", lambda: "x86_64")


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

    monkeypatch.setattr("openconstraint_mcp.runtime_install._download_archive", _fake_download)
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
    monkeypatch.setattr("openconstraint_mcp.runtime_install.sys.platform", "darwin")
    monkeypatch.setattr("openconstraint_mcp.runtime_install.platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeInstallError) as exc_info:
        install_managed_runtime(tmp_path / "runtime", console=_quiet_console())
    assert "Linux x86_64" in str(exc_info.value)


def test_install_rejects_unsupported_arch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.sys.platform", "linux")
    monkeypatch.setattr("openconstraint_mcp.runtime_install.platform.machine", lambda: "aarch64")
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
    runtime_install._write_runtime_marker(target)

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
    runtime_install._write_runtime_marker(target)
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
    runtime_install._write_runtime_marker(target)

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

    monkeypatch.setattr("openconstraint_mcp.runtime_install._smoke_check_binary", _boom)

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
    runtime_install._write_runtime_marker(target)
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
