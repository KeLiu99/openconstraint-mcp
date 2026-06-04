from __future__ import annotations

import hashlib
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

from .errors import RuntimeInstallError

MINIZINC_VERSION: str = "2.9.7"

# SHA256 of MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz from the upstream
# MiniZincIDE GitHub release. Recompute alongside MINIZINC_VERSION when bumping.
_ARCHIVE_SHA256: str = "7e78d3a1d6feec2f5b6a43628632decb6995755ade92ff4e51a2188c54ca6399"

_BUNDLE_FILENAME: str = f"MiniZincIDE-{MINIZINC_VERSION}-bundle-linux-x86_64.tgz"
_BUNDLE_URL: str = (
    f"https://github.com/MiniZinc/MiniZincIDE/releases/download/{MINIZINC_VERSION}/"
    f"{_BUNDLE_FILENAME}"
)


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
