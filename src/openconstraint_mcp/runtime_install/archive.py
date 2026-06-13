from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from .errors import RuntimeInstallError

_DMG_APP_DIR = "MiniZincIDE.app"
_DMG_RESOURCES_SUBPATH = Path("Contents") / "Resources"
# Verified layout of the 2.9.7 bundled .dmg: solver executables live in bin/,
# shared libraries in lib/, the stdlib and solver configs in share/minizinc/,
# and the minizinc/mzn2doc binaries sit at the Resources root.
_DMG_RUNTIME_DIRS = ("bin", "lib", "share")
_DMG_ROOT_BINARIES = ("minizinc", "mzn2doc")
_DMG_FRAMEWORKS_SUBPATH = Path("Contents") / "Frameworks"
# The bundled fzn-gecode is the Qt/Gist build: it loads QtCore/QtGui/QtWidgets/
# QtPrintSupport via @loader_path/../../Frameworks (two levels up from bin/),
# which resolves outside the reshaped runtime. They are vendored into
# <dest>/Frameworks and the references rewritten one level shallower so the
# solver loads headless; no GUI is launched.
_BUNDLE_FRAMEWORK_REF_PREFIX = "@loader_path/../../Frameworks/"
_RUNTIME_FRAMEWORK_REF_PREFIX = "@loader_path/../Frameworks/"


def _extract_tgz_bundle(archive: Path, dest: Path) -> None:
    """Extract ``archive`` into ``dest``, stripping the single top-level wrapper.

    The MiniZinc bundle ships everything under one wrapper directory
    (e.g. ``MiniZincIDE-2.9.7-bundle-linux-x86_64/``). After extraction the
    contents of that wrapper live directly under ``dest``.
    """
    scratch = dest / "_extract"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    try:
        try:
            with tarfile.open(archive, "r:*") as tar:
                tar.extractall(scratch, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeInstallError(
                f"failed to extract MiniZinc archive {archive}: {exc}"
            ) from exc

        entries = list(scratch.iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            raise RuntimeInstallError(
                "MiniZinc archive did not contain a single top-level directory "
                f"(got {[entry.name for entry in entries]})"
            )
        wrapper = entries[0]
        for child in wrapper.iterdir():
            shutil.move(child, dest / child.name)
        wrapper.rmdir()
    finally:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


def _attach_dmg(archive: Path, mountpoint: Path) -> None:
    """Mount ``archive`` read-only at ``mountpoint`` via ``hdiutil attach``.

    The explicit ``-mountpoint`` matters: the dmg volume name contains spaces
    and parentheses (``MiniZinc IDE 2.9.7 (bundled)``), so relying on the
    auto-mount path under /Volumes would be fragile.
    """
    cmd = [
        "hdiutil",
        "attach",
        str(archive),
        "-mountpoint",
        str(mountpoint),
        "-readonly",
        "-nobrowse",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeInstallError(
            "hdiutil not found; mounting the MiniZinc disk image requires macOS"
        ) from exc
    if result.returncode != 0:
        raise RuntimeInstallError(
            f"failed to mount MiniZinc disk image {archive}: {result.stderr.strip()}"
        )


def _detach_dmg(mountpoint: Path) -> str | None:
    """Unmount ``mountpoint``; return an error description instead of raising.

    Detach failure has different consequences depending on whether the copy
    succeeded, so the caller composes the final error from this value.
    """
    cmd = ["hdiutil", "detach", str(mountpoint)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return "hdiutil not found"
    if result.returncode != 0:
        return f"hdiutil detach {mountpoint} failed: {result.stderr.strip()}"
    return None


def _copy_dmg_runtime_tree(mountpoint: Path, dest: Path) -> None:
    """Copy the runtime tree out of the mounted image, reshaping it for ``dest``.

    ``bin/``, ``lib/``, and ``share/`` are copied as-is; the ``minizinc`` and
    ``mzn2doc`` binaries move from the ``Resources/`` root into ``dest/bin/``
    so the result matches the layout the runtime layer expects.
    """
    app_dir = mountpoint / _DMG_APP_DIR
    if not app_dir.is_dir():
        raise RuntimeInstallError(
            f"mounted MiniZinc disk image does not contain {_DMG_APP_DIR}"
        )
    resources = app_dir / _DMG_RESOURCES_SUBPATH
    if not resources.is_dir():
        raise RuntimeInstallError(
            "mounted MiniZinc disk image does not contain "
            f"{_DMG_APP_DIR}/{_DMG_RESOURCES_SUBPATH.as_posix()}"
        )
    expected = (*_DMG_RUNTIME_DIRS, *_DMG_ROOT_BINARIES)
    missing = [name for name in expected if not (resources / name).exists()]
    if missing:
        raise RuntimeInstallError(
            "mounted MiniZinc disk image is missing expected entries under "
            f"{_DMG_APP_DIR}/{_DMG_RESOURCES_SUBPATH.as_posix()}: {', '.join(missing)}"
        )
    try:
        for name in _DMG_RUNTIME_DIRS:
            shutil.copytree(resources / name, dest / name, symlinks=True)
        for name in _DMG_ROOT_BINARIES:
            shutil.copy2(resources / name, dest / "bin" / name)
    except OSError as exc:
        raise RuntimeInstallError(
            f"failed to copy the MiniZinc runtime out of the mounted disk image: {exc}"
        ) from exc

    _vendor_gecode_qt_frameworks(app_dir, dest)


def _macho_bundle_framework_refs(binary: Path) -> list[str]:
    """Return *binary*'s ``@loader_path/../../Frameworks/...`` install names.

    These point at the app bundle's ``Contents/Frameworks`` directory, which
    sits outside the reshaped runtime; an empty list means the binary needs no
    vendored frameworks. Duplicate references (one per slice of a universal
    binary) are collapsed. Raises :class:`RuntimeInstallError` if ``otool`` is
    unavailable.
    """
    try:
        result = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeInstallError(
            "keeping the Gecode solver on macOS needs the Xcode command line "
            "tools (otool); install them with `xcode-select --install` and "
            "re-run install-runtime."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeInstallError(
            f"could not inspect {binary.name} with otool: {(exc.stderr or '').strip()}"
        ) from exc
    refs: list[str] = []
    for line in result.stdout.splitlines():
        token = line.strip().split(" ", 1)[0]
        if token.startswith(_BUNDLE_FRAMEWORK_REF_PREFIX):
            refs.append(token)
    return list(dict.fromkeys(refs))


def _run_macho_relink(cmd: list[str]) -> None:
    """Run a relink/sign step (``install_name_tool``/``codesign``) with clear errors."""
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeInstallError(
            f"keeping the Gecode solver on macOS needs {cmd[0]} (part of the "
            "Xcode command line tools); install them with `xcode-select "
            "--install` and re-run install-runtime."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeInstallError(
            f"failed to relink the Gecode solver with {cmd[0]}: {(exc.stderr or '').strip()}"
        ) from exc


def _framework_binary(framework_dir: Path) -> Path | None:
    """Return the Mach-O binary inside a ``*.framework`` directory, if present."""
    name = framework_dir.name.removesuffix(".framework")
    for candidate in (
        framework_dir / "Versions" / "A" / name,
        framework_dir / "Versions" / "Current" / name,
        framework_dir / name,
    ):
        if candidate.is_file():
            return candidate
    return None


def _macho_framework_dependency_names(binary: Path) -> set[str]:
    """Return the ``*.framework`` directory names *binary* depends on.

    Reads ``otool -L`` and keeps only ``@``-relative framework references
    (``@rpath``/``@loader_path``/``@executable_path``); system frameworks under
    ``/System`` and plain ``/usr/lib`` dylibs are ignored. Best-effort: a
    non-Mach-O file yields an empty set rather than raising.
    """
    result = subprocess.run(
        ["otool", "-L", str(binary)], capture_output=True, text=True, check=False
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        token = line.strip().split(" ", 1)[0]
        if not token.startswith("@"):
            continue
        match = re.search(r"/([^/]+\.framework)/", token)
        if match:
            names.add(match.group(1))
    return names


def _resolve_framework_closure(frameworks_src: Path, seed: set[str]) -> set[str]:
    """Return the transitive set of bundle frameworks reachable from *seed*.

    Qt frameworks depend on one another (e.g. QtGui -> QtDBus), and each is
    copied wholesale, so missing a transitive link leaves the solver unable to
    load. Only frameworks present in the bundle are followed; a *seed* framework
    that is absent is an error, since the solver links it directly.
    """
    closure: set[str] = set()
    queue = list(seed)
    while queue:
        framework = queue.pop()
        if framework in closure:
            continue
        source = frameworks_src / framework
        if not source.is_dir():
            if framework in seed:
                raise RuntimeInstallError(
                    f"the mounted MiniZinc disk image is missing the Qt framework "
                    f"{framework} that the Gecode solver links "
                    f"({_DMG_APP_DIR}/{_DMG_FRAMEWORKS_SUBPATH.as_posix()}); cannot "
                    "keep Gecode on the macOS managed runtime."
                )
            continue
        closure.add(framework)
        binary = _framework_binary(source)
        if binary is not None:
            queue.extend(_macho_framework_dependency_names(binary))
    return closure


def _vendor_gecode_qt_frameworks(app_dir: Path, dest: Path) -> None:
    """Vendor the Qt frameworks the Gecode solver binaries link, into the runtime.

    The bundled ``fzn-gecode`` is the Qt/Gist build: it loads
    QtCore/QtGui/QtWidgets/QtPrintSupport through
    ``@loader_path/../../Frameworks`` (every other dependency is a system
    framework), which resolves outside the reshaped ``dest`` tree. Copy each
    referenced framework — and its transitive Qt dependencies — into
    ``dest/Frameworks`` and rewrite the solver
    binaries' references one level shallower (``@loader_path/../Frameworks``)
    so they resolve in place, then ad-hoc re-sign — an install-name edit
    invalidates the macOS signature. No GUI is launched: only the linked
    libraries are vendored, so headless solving works while Gist stays unused.

    Bundles without an app-level ``Contents/Frameworks`` (the Linux tarball,
    or test fixtures) need no vendoring and return early. Raises
    :class:`RuntimeInstallError` if a referenced framework is absent or the
    relink tools are unavailable.
    """
    frameworks_src = app_dir / _DMG_FRAMEWORKS_SUBPATH
    if not frameworks_src.is_dir():
        return

    binaries_with_refs: dict[Path, list[str]] = {}
    for candidate in sorted((dest / "bin").iterdir()):
        if candidate.is_file() and not candidate.is_symlink():
            refs = _macho_bundle_framework_refs(candidate)
            if refs:
                binaries_with_refs[candidate] = refs
    if not binaries_with_refs:
        return

    seed = {
        ref[len(_BUNDLE_FRAMEWORK_REF_PREFIX) :].split("/", 1)[0]
        for refs in binaries_with_refs.values()
        for ref in refs
    }
    closure = _resolve_framework_closure(frameworks_src, seed)
    dest_frameworks = dest / "Frameworks"
    dest_frameworks.mkdir(exist_ok=True)
    for framework in sorted(closure):
        shutil.copytree(frameworks_src / framework, dest_frameworks / framework, symlinks=True)

    for binary, refs in binaries_with_refs.items():
        for ref in refs:
            relinked = _RUNTIME_FRAMEWORK_REF_PREFIX + ref[len(_BUNDLE_FRAMEWORK_REF_PREFIX) :]
            _run_macho_relink(["install_name_tool", "-change", ref, relinked, str(binary)])
        _run_macho_relink(["codesign", "--force", "--sign", "-", str(binary)])


def _extract_dmg_bundle(archive: Path, dest: Path) -> None:
    """Extract the MiniZinc runtime from the macOS ``.dmg`` bundle into ``dest``."""
    scratch = dest / "_dmg"
    if scratch.exists():
        shutil.rmtree(scratch)
    mountpoint = scratch / "mount"
    mountpoint.mkdir(parents=True)
    try:
        _attach_dmg(archive, mountpoint)
        copy_error: RuntimeInstallError | None = None
        try:
            _copy_dmg_runtime_tree(mountpoint, dest)
        except RuntimeInstallError as exc:
            copy_error = exc
        except BaseException:
            # Unexpected exit mid-copy (e.g. KeyboardInterrupt): best-effort
            # detach so the image is not left mounted, then re-raise as-is.
            _detach_dmg(mountpoint)
            raise
        detach_error = _detach_dmg(mountpoint)
        if copy_error is not None:
            if detach_error is not None:
                raise RuntimeInstallError(
                    f"{copy_error} (cleanup also failed: {detach_error})"
                ) from copy_error
            raise copy_error
        if detach_error is not None:
            raise RuntimeInstallError(
                f"MiniZinc runtime copied, but the disk image could not be "
                f"unmounted: {detach_error}"
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
