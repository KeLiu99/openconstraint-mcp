from __future__ import annotations

import io
import os
import subprocess
import tarfile
from collections.abc import Callable, Collection
from pathlib import Path

import pytest

from openconstraint_mcp.runtime_install.archive import (
    _extract_dmg_bundle,
    _extract_tgz_bundle,
    _install_nsis_bundle,
    _vendor_gecode_qt_frameworks,
)
from openconstraint_mcp.runtime_install.errors import RuntimeInstallError


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


def test_fake_minizinc_tarball_has_single_wrapper_dir(fake_minizinc_tarball: Path) -> None:
    with tarfile.open(fake_minizinc_tarball, "r:gz") as tar:
        names = tar.getnames()
    top_level = {name.split("/", 1)[0] for name in names}
    assert len(top_level) == 1, f"expected one top-level dir, got {top_level}"
    assert any(name.endswith("/bin/minizinc") for name in names)


def test_extract_bundle_strips_wrapper(
    fake_minizinc_tarball: Path,
    tmp_path: Path,
) -> None:
    dest = tmp_path / "runtime"
    dest.mkdir()
    _extract_tgz_bundle(fake_minizinc_tarball, dest)

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
        _extract_tgz_bundle(archive, dest)


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
        _extract_tgz_bundle(archive, dest)


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
        _extract_tgz_bundle(archive, dest)


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
        _extract_tgz_bundle(archive, dest)


def _write_executable(path: Path) -> None:
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)


def _build_fake_mounted_volume(mountpoint: Path, *, omit: Collection[str] = ()) -> None:
    """Mirror the verified MiniZincIDE-2.9.7-bundled.dmg layout under ``mountpoint``.

    ``omit`` removes entries from the canonical layout so each missing-entry
    test diverges from the verified-good shape by exactly one item:
    ``"app"`` skips MiniZincIDE.app entirely, ``"resources"`` stops at
    ``Contents/``, ``"minizinc"`` drops the root minizinc binary.
    """
    if "app" in omit:
        return
    contents = mountpoint / "MiniZincIDE.app" / "Contents"
    if "resources" in omit:
        contents.mkdir(parents=True)
        return
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    if "minizinc" not in omit:
        _write_executable(resources / "minizinc")
    _write_executable(resources / "mzn2doc")
    bin_dir = resources / "bin"
    bin_dir.mkdir()
    for solver in ("fzn-cp-sat", "fzn-chuffed"):
        _write_executable(bin_dir / solver)
    lib_dir = resources / "lib"
    lib_dir.mkdir()
    (lib_dir / "libhighs.dylib").write_bytes(b"\x00")
    (lib_dir / "libhighs.1.dylib").symlink_to("libhighs.dylib")
    solvers = resources / "share" / "minizinc" / "solvers"
    solvers.mkdir(parents=True)
    (solvers / "cp-sat.msc").write_text("{}")
    std = resources / "share" / "minizinc" / "std"
    std.mkdir()
    (std / "globals.mzn").write_text("% stdlib\n")


class _FakeHdiutil:
    """Stand-in for ``subprocess.run`` emulating ``hdiutil attach``/``detach``.

    On a successful attach it reads the mountpoint back out of the received
    argv and runs ``populate`` there, so a bug in argv construction (e.g. a
    dropped ``-mountpoint``) fails loudly instead of passing silently.
    """

    def __init__(
        self,
        populate: Callable[[Path], None],
        *,
        attach_returncode: int = 0,
        detach_returncode: int = 0,
    ) -> None:
        self._populate = populate
        self._attach_returncode = attach_returncode
        self._detach_returncode = detach_returncode
        self.attach_calls: list[list[str]] = []
        self.detach_calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "hdiutil", f"unexpected subprocess call: {cmd}"
        if cmd[1] == "attach":
            self.attach_calls.append(cmd)
            if self._attach_returncode != 0:
                return subprocess.CompletedProcess(
                    cmd, self._attach_returncode, stdout="", stderr="hdiutil: attach failed"
                )
            mountpoint = Path(cmd[cmd.index("-mountpoint") + 1])
            self._populate(mountpoint)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[1] == "detach":
            self.detach_calls.append(cmd)
            stderr = "" if self._detach_returncode == 0 else "hdiutil: detach failed"
            return subprocess.CompletedProcess(
                cmd, self._detach_returncode, stdout="", stderr=stderr
            )
        raise AssertionError(f"unexpected hdiutil verb: {cmd}")


def _install_fake_hdiutil(monkeypatch: pytest.MonkeyPatch, fake: _FakeHdiutil) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", fake)


def test_extract_dmg_bundle_moves_root_binaries_into_bin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)

    binary = dest / "bin" / "minizinc"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    assert (dest / "bin" / "mzn2doc").is_file()


def test_extract_dmg_bundle_places_lib_and_share_beside_bin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)

    assert (dest / "bin" / "fzn-cp-sat").is_file()
    assert (dest / "lib" / "libhighs.dylib").is_file()
    assert (dest / "share" / "minizinc" / "solvers" / "cp-sat.msc").is_file()
    assert (dest / "share" / "minizinc" / "std" / "globals.mzn").is_file()


def test_extract_dmg_bundle_preserves_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)

    link = dest / "lib" / "libhighs.1.dylib"
    assert link.is_symlink()
    assert os.readlink(link) == "libhighs.dylib"


def test_extract_dmg_bundle_detaches_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)

    assert fake.detach_calls == [["hdiutil", "detach", str(dest / "_dmg" / "mount")]]


def test_extract_dmg_bundle_removes_scratch_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)

    assert not (dest / "_dmg").exists()


def test_extract_dmg_bundle_missing_hdiutil(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_hdiutil(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("hdiutil")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", _no_hdiutil)
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert "hdiutil" in str(exc_info.value)


def test_extract_dmg_bundle_attach_failure_leaves_no_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume, attach_returncode=1)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert "mount" in str(exc_info.value).lower()
    assert not (dest / "bin").exists()
    assert fake.detach_calls == []


def test_extract_dmg_bundle_missing_app_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _populate(mountpoint: Path) -> None:
        _build_fake_mounted_volume(mountpoint, omit={"app"})

    _install_fake_hdiutil(monkeypatch, _FakeHdiutil(_populate))
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert "MiniZincIDE.app" in str(exc_info.value)


def test_extract_dmg_bundle_missing_resources_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _populate(mountpoint: Path) -> None:
        _build_fake_mounted_volume(mountpoint, omit={"resources"})

    _install_fake_hdiutil(monkeypatch, _FakeHdiutil(_populate))
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert "MiniZincIDE.app/Contents/Resources" in str(exc_info.value)


def test_extract_dmg_bundle_missing_minizinc_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _populate(mountpoint: Path) -> None:
        _build_fake_mounted_volume(mountpoint, omit={"minizinc"})

    _install_fake_hdiutil(monkeypatch, _FakeHdiutil(_populate))
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    message = str(exc_info.value)
    assert "missing expected entries" in message
    assert "minizinc" in message


def test_extract_dmg_bundle_detaches_when_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _populate(mountpoint: Path) -> None:
        _build_fake_mounted_volume(mountpoint, omit={"minizinc"})

    fake = _FakeHdiutil(_populate)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError):
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert len(fake.detach_calls) == 1


def test_extract_dmg_bundle_detaches_on_interrupt_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume)
    _install_fake_hdiutil(monkeypatch, fake)

    def _interrupt(mountpoint: Path, dest: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.archive._copy_dmg_runtime_tree", _interrupt
    )
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(KeyboardInterrupt):
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert len(fake.detach_calls) == 1


def test_extract_dmg_bundle_detach_failure_after_copy_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHdiutil(_build_fake_mounted_volume, detach_returncode=1)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    assert "detach" in str(exc_info.value)
    # The copy itself succeeded before the detach failure.
    assert (dest / "bin" / "minizinc").is_file()


def test_extract_dmg_bundle_detach_failure_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _populate(mountpoint: Path) -> None:
        _build_fake_mounted_volume(mountpoint, omit={"minizinc"})

    fake = _FakeHdiutil(_populate, detach_returncode=1)
    _install_fake_hdiutil(monkeypatch, fake)
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _extract_dmg_bundle(tmp_path / "bundle.dmg", dest)
    message = str(exc_info.value)
    assert "missing expected entries" in message
    assert "detach" in message


_GECODE_QT = ("QtCore", "QtGui", "QtWidgets", "QtPrintSupport")


def _qt_ref(name: str) -> str:
    return f"@loader_path/../../Frameworks/{name}.framework/Versions/A/{name}"


def _otool_output(binary: Path, framework_refs: list[str]) -> str:
    """Render an ``otool -L`` stdout block for *binary* listing *framework_refs*."""
    lines = [f"{binary}:"]
    lines += [f"\t{ref} (compatibility version 6.0.0)" for ref in framework_refs]
    lines.append("\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0)")
    return "\n".join(lines) + "\n"


class _FakeMachOTools:
    """``subprocess.run`` stand-in for ``otool``/``install_name_tool``/``codesign``.

    ``otool_refs`` maps a binary basename to the bundle-Frameworks references its
    ``otool -L`` reports; an absent binary reports only a system dylib.
    """

    def __init__(self, otool_refs: dict[str, list[str]]) -> None:
        self._otool_refs = otool_refs
        self.install_name_tool_calls: list[list[str]] = []
        self.codesign_calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        tool = cmd[0]
        if tool == "otool":
            binary = Path(cmd[-1])
            stdout = _otool_output(binary, self._otool_refs.get(binary.name, []))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if tool == "install_name_tool":
            self.install_name_tool_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if tool == "codesign":
            self.codesign_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected tool call: {cmd}")


def _build_app_with_frameworks(app_dir: Path, frameworks: Collection[str]) -> None:
    """Create ``app_dir/Contents/Frameworks/<name>.framework`` stand-ins."""
    fw_root = app_dir / "Contents" / "Frameworks"
    fw_root.mkdir(parents=True)
    for name in frameworks:
        versions = fw_root / f"{name}.framework" / "Versions" / "A"
        versions.mkdir(parents=True)
        (versions / name).write_bytes(b"\x00")


def test_vendor_copies_frameworks_and_relinks_gecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "MiniZincIDE.app"
    _build_app_with_frameworks(app_dir, _GECODE_QT)
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")
    _write_executable(dest / "bin" / "fzn-cp-sat")

    fake = _FakeMachOTools({"fzn-gecode": [_qt_ref(name) for name in _GECODE_QT]})
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", fake)

    _vendor_gecode_qt_frameworks(app_dir, dest)

    for name in _GECODE_QT:
        assert (dest / "Frameworks" / f"{name}.framework" / "Versions" / "A" / name).is_file()
    # Only fzn-gecode is relinked; each Qt reference is repointed one level shallower.
    assert {cmd[-1] for cmd in fake.install_name_tool_calls} == {str(dest / "bin" / "fzn-gecode")}
    assert {cmd[2] for cmd in fake.install_name_tool_calls} == {_qt_ref(n) for n in _GECODE_QT}
    assert all(
        cmd[3].startswith("@loader_path/../Frameworks/") for cmd in fake.install_name_tool_calls
    )
    assert [cmd[-1] for cmd in fake.codesign_calls] == [str(dest / "bin" / "fzn-gecode")]


def test_vendor_dedupes_universal_binary_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "MiniZincIDE.app"
    _build_app_with_frameworks(app_dir, ["QtCore"])
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")
    # otool lists the reference once per universal slice; the relink runs once.
    fake = _FakeMachOTools({"fzn-gecode": [_qt_ref("QtCore"), _qt_ref("QtCore")]})
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", fake)

    _vendor_gecode_qt_frameworks(app_dir, dest)

    assert len(fake.install_name_tool_calls) == 1
    assert len(fake.codesign_calls) == 1


def test_vendor_is_noop_without_app_frameworks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "MiniZincIDE.app"
    (app_dir / "Contents").mkdir(parents=True)  # no Frameworks/ (Linux-style tree)
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")

    def _forbidden(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"vendoring must not shell out here: {cmd}")

    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", _forbidden)

    _vendor_gecode_qt_frameworks(app_dir, dest)

    assert not (dest / "Frameworks").exists()


def test_vendor_missing_dev_tools_reports_xcode_clt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "MiniZincIDE.app"
    _build_app_with_frameworks(app_dir, ["QtCore"])
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")

    def _no_tool(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", _no_tool)

    with pytest.raises(RuntimeInstallError) as exc_info:
        _vendor_gecode_qt_frameworks(app_dir, dest)
    assert "xcode-select" in str(exc_info.value)


def test_vendor_missing_framework_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_dir = tmp_path / "MiniZincIDE.app"
    _build_app_with_frameworks(app_dir, ["QtCore"])  # QtGui is absent from the bundle
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")
    fake = _FakeMachOTools({"fzn-gecode": [_qt_ref("QtCore"), _qt_ref("QtGui")]})
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", fake)

    with pytest.raises(RuntimeInstallError) as exc_info:
        _vendor_gecode_qt_frameworks(app_dir, dest)
    assert "QtGui" in str(exc_info.value)


def test_vendor_follows_transitive_framework_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fzn-gecode links QtCore + QtGui directly; QtGui in turn pulls in QtDBus
    # via @rpath, so QtDBus must be vendored too even though the solver never
    # names it. Mirrors the real 2.9.7 bundle.
    app_dir = tmp_path / "MiniZincIDE.app"
    _build_app_with_frameworks(app_dir, ["QtCore", "QtGui", "QtDBus"])
    dest = tmp_path / "runtime"
    (dest / "bin").mkdir(parents=True)
    _write_executable(dest / "bin" / "fzn-gecode")
    fake = _FakeMachOTools(
        {
            "fzn-gecode": [_qt_ref("QtCore"), _qt_ref("QtGui")],
            "QtGui": ["@rpath/QtDBus.framework/Versions/A/QtDBus"],
        }
    )
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.run", fake)

    _vendor_gecode_qt_frameworks(app_dir, dest)

    assert (dest / "Frameworks" / "QtDBus.framework" / "Versions" / "A" / "QtDBus").is_file()


# ---------------------------------------------------------------------------
# Windows NSIS silent install


class _FakeNsisProc:
    """Stand-in for the ``subprocess.Popen`` handle the installer call returns.

    ``wait`` reports the configured exit code, or raises
    :class:`subprocess.TimeoutExpired` when primed to mimic the headless-UAC
    hang the real bound guards against.
    """

    def __init__(self, *, returncode: int, timeout: bool) -> None:
        self.pid = 4321
        self.returncode = returncode
        self._timeout = timeout

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="installer", timeout=timeout)
        return self.returncode


class _FakeNsisInstaller:
    """``subprocess.Popen`` stand-in for the Windows NSIS installer.

    Records the command-line *string* it received and writes the configured
    output to the redirect file the implementation passes as ``stdout`` — so the
    diagnostics path reads back real bytes from its temp log. On a zero return
    code it materializes the runtime tree at the ``/D=`` destination read back
    out of that command, so a bug in command construction (a dropped or quoted
    ``/D=``) fails loudly instead of passing silently. ``populate`` can omit the
    binary to drive the missing-tree validation path; ``timeout`` primes the
    returned handle's ``wait`` to raise :class:`subprocess.TimeoutExpired`.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        populate: bool = True,
        stdout: str = "",
        stderr: str = "",
        timeout: bool = False,
    ) -> None:
        self._returncode = returncode
        self._populate = populate
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self.commands: list[str] = []
        self.proc: _FakeNsisProc | None = None

    def __call__(self, command: str, **kwargs: object) -> _FakeNsisProc:
        assert isinstance(command, str), f"NSIS install must use a command string, got {command!r}"
        self.commands.append(command)
        # The implementation redirects stdout+stderr to one file; mirror that by
        # writing the combined output to the sink it passes as ``stdout``.
        sink = kwargs.get("stdout")
        if sink is not None and (self._stdout or self._stderr):
            sink.write(self._stdout + self._stderr)  # type: ignore[attr-defined]
        if self._returncode == 0 and self._populate and not self._timeout:
            dest = Path(command.split("/D=", 1)[1])
            bin_dir = dest / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "minizinc.exe").write_bytes(b"MZ\x00fake")
            (dest / "share" / "minizinc").mkdir(parents=True, exist_ok=True)
        self.proc = _FakeNsisProc(returncode=self._returncode, timeout=self._timeout)
        return self.proc


def _install_fake_nsis(monkeypatch: pytest.MonkeyPatch, fake: _FakeNsisInstaller) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.archive.subprocess.Popen", fake)


def test_install_nsis_bundle_runs_silent_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNsisInstaller()
    _install_fake_nsis(monkeypatch, fake)
    installer = tmp_path / "MiniZincIDE-2.9.7-bundled-setup-win64.exe"
    installer.write_bytes(b"installer")
    dest = tmp_path / "staging"
    dest.mkdir()

    _install_nsis_bundle(installer, dest)

    assert len(fake.commands) == 1
    command = fake.commands[0]
    # exe quoted, /S present, /D= trailing and unquoted.
    assert command.startswith(f'"{installer}"')
    assert "/S" in command
    assert command.endswith(f"/D={dest}")
    assert '/D="' not in command
    # Validation accepted the materialized tree.
    assert (dest / "bin" / "minizinc.exe").is_file()


def test_install_nsis_bundle_path_with_spaces_stays_unquoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNsisInstaller()
    _install_fake_nsis(monkeypatch, fake)
    installer = tmp_path / "setup-win64.exe"
    installer.write_bytes(b"installer")
    # A parent with a space mimics C:\Program Files\... — the load-bearing case.
    dest = tmp_path / "Program Files" / "runtime"
    dest.parent.mkdir(parents=True)
    dest.mkdir()

    _install_nsis_bundle(installer, dest)

    command = fake.commands[0]
    # The /D= value must be the literal spaced path with no surrounding quotes;
    # NSIS reads to end-of-line, and quoting would corrupt the install path.
    assert command.endswith(f"/D={dest}")
    assert '/D="' not in command
    assert f'"{dest}"' not in command


def test_install_nsis_bundle_nonzero_exit_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNsisInstaller(returncode=1, stderr="SmartScreen blocked the installer")
    _install_fake_nsis(monkeypatch, fake)
    installer = tmp_path / "setup-win64.exe"
    installer.write_bytes(b"installer")
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _install_nsis_bundle(installer, dest)
    message = str(exc_info.value)
    assert "1" in message
    assert "SmartScreen blocked the installer" in message


def test_install_nsis_bundle_missing_binary_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Installer reports success but leaves no bin\minizinc.exe behind.
    fake = _FakeNsisInstaller(populate=False)
    _install_fake_nsis(monkeypatch, fake)
    installer = tmp_path / "setup-win64.exe"
    installer.write_bytes(b"installer")
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _install_nsis_bundle(installer, dest)
    assert "minizinc.exe" in str(exc_info.value)


def test_install_nsis_bundle_timeout_kills_tree_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A headless runner can't answer the installer's UAC prompt, so the elevated
    # grandchild hangs; the bound must time out, kill the process tree, and
    # surface the elevation cause.
    fake = _FakeNsisInstaller(timeout=True)
    _install_fake_nsis(monkeypatch, fake)
    killed: list[int] = []
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.archive._kill_process_tree",
        lambda pid: killed.append(pid),
    )
    installer = tmp_path / "setup-win64.exe"
    installer.write_bytes(b"installer")
    dest = tmp_path / "staging"
    dest.mkdir()

    with pytest.raises(RuntimeInstallError) as exc_info:
        _install_nsis_bundle(installer, dest)

    message = str(exc_info.value).lower()
    assert "minutes" in message
    assert "elevat" in message
    assert fake.proc is not None
    assert killed == [fake.proc.pid]
