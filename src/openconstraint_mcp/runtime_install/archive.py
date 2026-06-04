from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from .download import RuntimeInstallError


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
