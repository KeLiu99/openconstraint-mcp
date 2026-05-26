from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

MINIZINC_VERSION: str = "2.9.7"

# SHA256 of MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz from the upstream
# MiniZincIDE GitHub release. Recompute alongside MINIZINC_VERSION when bumping.
_ARCHIVE_SHA256: str = "7e78d3a1d6feec2f5b6a43628632decb6995755ade92ff4e51a2188c54ca6399"

_BUNDLE_FILENAME: str = f"MiniZincIDE-{MINIZINC_VERSION}-bundle-linux-x86_64.tgz"
_BUNDLE_URL: str = (
    f"https://github.com/MiniZinc/MiniZincIDE/releases/download/{MINIZINC_VERSION}/"
    f"{_BUNDLE_FILENAME}"
)

MANAGED_RUNTIME_MARKER: str = ".openconstraint-runtime.json"


class RuntimeInstallError(RuntimeError):
    """Raised when installing the managed MiniZinc runtime fails."""


def check_supported_platform() -> None:
    """Raise :class:`RuntimeInstallError` on anything other than Linux x86_64."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeInstallError(
            "openconstraint-mcp install-runtime currently supports Linux x86_64 only. "
            "On other platforms, install MiniZinc manually and point "
            "OPENCONSTRAINT_MCP_RUNTIME_DIR at the directory containing bin/minizinc."
        )


def is_managed_runtime_dir(path: Path) -> bool:
    """True iff ``path/.openconstraint-runtime.json`` parses as a managed-install marker."""
    marker = path / MANAGED_RUNTIME_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("managed_by") == "openconstraint-mcp"


def _write_runtime_marker(runtime_dir: Path) -> None:
    marker = runtime_dir / MANAGED_RUNTIME_MARKER
    payload = {
        "managed_by": "openconstraint-mcp",
        "minizinc_version": MINIZINC_VERSION,
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n")


def _download_archive(
    url: str,
    dest: Path,
    expected_sha256: str,
    console: Console,
) -> None:
    """Stream ``url`` to ``dest`` with a rich progress bar and SHA256 verify.

    On any HTTP error or checksum mismatch ``dest`` is unlinked and a
    :class:`RuntimeInstallError` is raised so callers never see a partial or
    tampered file on disk.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    hasher = hashlib.sha256()
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise RuntimeInstallError(
                        f"download failed: HTTP {response.status_code} from {url}"
                    )
                total = int(response.headers.get("content-length", 0)) or None
                with Progress(
                    TextColumn("[bold blue]Downloading"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as progress:
                    task_id = progress.add_task("download", total=total)
                    with dest.open("wb") as fh:
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            fh.write(chunk)
                            hasher.update(chunk)
                            progress.update(task_id, advance=len(chunk))
    except RuntimeInstallError:
        if dest.exists():
            dest.unlink()
        raise
    except httpx.HTTPError as exc:
        if dest.exists():
            dest.unlink()
        raise RuntimeInstallError(f"download failed for {url}: {exc}") from exc

    digest = hasher.hexdigest()
    if digest.lower() != expected_sha256.lower():
        if dest.exists():
            dest.unlink()
        raise RuntimeInstallError(
            "checksum mismatch for downloaded MiniZinc archive: "
            f"expected {expected_sha256}, got {digest}"
        )


def _extract_bundle(archive: Path, dest: Path) -> None:
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


def _smoke_check_binary(binary: Path) -> None:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeInstallError(
            f"staged MiniZinc binary is missing or not executable: {binary}"
        )
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeInstallError(
            f"staged MiniZinc binary exited {exc.returncode} on --version: "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeInstallError(
            f"staged MiniZinc binary timed out on --version: {binary}"
        ) from exc
    except OSError as exc:
        raise RuntimeInstallError(
            f"could not execute staged MiniZinc binary {binary}: {exc}"
        ) from exc

    output = (completed.stdout or "") + (completed.stderr or "")
    if "minizinc" not in output.lower():
        raise RuntimeInstallError(
            f"staged MiniZinc binary did not identify itself as MiniZinc: "
            f"{output.strip()!r}"
        )


def _remove_stale_staging(parent: Path, name: str) -> None:
    # Assumes no concurrent install-runtime for the same target (the v0 contract):
    # this clears *any* matching staging sibling, including one a second live
    # install would own.
    prefix = f".{name}.staging."
    for candidate in parent.iterdir():
        if candidate.name.startswith(prefix):
            shutil.rmtree(candidate, ignore_errors=True)


def _check_stale_backup(parent: Path, runtime_dir: Path) -> None:
    prefix = f".{runtime_dir.name}.backup."
    for candidate in parent.iterdir():
        if not candidate.name.startswith(prefix):
            continue
        if runtime_dir.exists():
            raise RuntimeInstallError(
                f"found a stale backup directory at {candidate} alongside an "
                f"existing managed runtime at {runtime_dir}. The previous install "
                "succeeded but its cleanup step failed; the backup is dead "
                f"weight. Remove it with `rm -rf {candidate}` and re-run "
                "install-runtime."
            )
        raise RuntimeInstallError(
            f"found a stale backup directory at {candidate} and no runtime at "
            f"{runtime_dir} — a previous install-runtime was interrupted "
            "mid-swap. Recover by running "
            f"`mv {candidate} {runtime_dir}` to restore the prior runtime, then "
            "re-run install-runtime."
        )


def install_managed_runtime(
    runtime_dir: Path,
    *,
    yes: bool = False,
    console: Console | None = None,
) -> Path:
    """Install the managed MiniZinc runtime into ``runtime_dir`` and return it."""
    if console is None:
        console = Console(quiet=True)

    check_supported_platform()

    runtime_dir = runtime_dir.resolve()
    if runtime_dir.exists() and not runtime_dir.is_dir():
        raise RuntimeInstallError(
            f"target exists but is not a directory: {runtime_dir}"
        )

    if runtime_dir.exists() and any(runtime_dir.iterdir()):
        if not is_managed_runtime_dir(runtime_dir):
            raise RuntimeInstallError(
                f"refusing to overwrite {runtime_dir}: this directory is not "
                "empty and does not look like a prior managed install (no "
                f"{MANAGED_RUNTIME_MARKER} marker). Pick an empty directory or "
                "remove the contents yourself before re-running install-runtime."
            )
        if not yes:
            raise RuntimeInstallError(
                f"refusing to overwrite non-empty runtime directory {runtime_dir}; "
                "re-run with --yes to replace a prior managed install."
            )

    parent = runtime_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_staging(parent, runtime_dir.name)
    _check_stale_backup(parent, runtime_dir)

    pid = os.getpid()
    staging = parent / f".{runtime_dir.name}.staging.{pid}"
    backup = parent / f".{runtime_dir.name}.backup.{pid}"

    # Phase 1 — no target mutation. Download, extract, smoke-check into staging.
    try:
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="oc-mcp-dl-") as tmp_dir:
            archive_path = Path(tmp_dir) / _BUNDLE_FILENAME
            _download_archive(_BUNDLE_URL, archive_path, _ARCHIVE_SHA256, console)
            _extract_bundle(archive_path, staging)
        _smoke_check_binary(staging / "bin" / "minizinc")
        _write_runtime_marker(staging)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # Phase 2 — rollback-safe swap.
    moved_aside = False
    target_swapped = False
    try:
        if runtime_dir.exists():
            runtime_dir.rename(backup)
            moved_aside = True
        staging.rename(runtime_dir)
        target_swapped = True
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if not target_swapped and moved_aside and backup.exists():
            try:
                backup.rename(runtime_dir)
            except OSError as restore_exc:
                raise RuntimeInstallError(
                    f"install failed and the prior runtime could not be restored "
                    f"from {backup}. Recover manually with "
                    f"`mv {backup} {runtime_dir}`."
                ) from restore_exc
        raise

    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as cleanup_exc:
            console.print(
                f"[yellow]Warning:[/yellow] install succeeded but backup at "
                f"{backup} could not be removed ({cleanup_exc}). Remove it "
                f"manually with `rm -rf {backup}`."
            )

    console.print(
        f"[green]Installed MiniZinc {MINIZINC_VERSION} at {runtime_dir}[/green]"
    )
    return runtime_dir
