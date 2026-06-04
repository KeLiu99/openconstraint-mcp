from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from openconstraint_mcp.runtime_install.archive import _extract_bundle
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
    _extract_bundle(fake_minizinc_tarball, dest)

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
        _extract_bundle(archive, dest)


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
        _extract_bundle(archive, dest)


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
        _extract_bundle(archive, dest)


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
        _extract_bundle(archive, dest)
