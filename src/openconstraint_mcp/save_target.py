"""Shared save-target validation, manifest I/O, and atomic staging-swap.

Importable by both ``minizinc`` and ``pyexec`` without coupling those subtrees.
Dependencies: stdlib + Pydantic + schemas only. Never imports minizinc.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from .schemas import SavedModelArtifact

# The manifest doubles as the managed-directory marker: only a directory whose
# marker parses (see _prior_manifest_filenames) may ever be overwritten.
MANIFEST_FILENAME: str = ".openconstraint-model.json"

_MANIFEST_MANAGED_BY: str = "openconstraint-mcp"
_PACKAGE_NAME: str = "openconstraint-mcp"


def tool_version() -> str:
    """Return the installed package version, or ``"unknown"`` if unavailable."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def text_sha256(text: str) -> str:
    """Return the sha256 hex digest of ``text``, encoded as UTF-8.

    Deliberately no newline normalization and no trimming — ``text`` is hashed
    exactly as given. This is distinct from the ``_sha256_of(path)`` helpers in
    ``pyexec/save.py``/``minizinc/artifacts.py``, which hash file bytes read back
    from disk; this helper hashes an in-memory string directly, so a caller that
    hashes a request's ``source``/``checker``/``problem`` text and a later save-path
    consistency check that hashes the same string are guaranteed to agree — a file
    write can alter line endings per platform, but this helper never touches a file.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prior_manifest_filenames(target: Path) -> list[str] | None:
    """Return the artifact filenames a prior save's manifest lists, or ``None``.

    ``None`` means "not a managed save directory": no marker file, unreadable
    or non-JSON content, the wrong ``managed_by`` identity, or a malformed
    ``artifacts`` list. Fail-closed on purpose — an unrecognizable manifest
    makes the overwrite gate refuse the directory rather than guess which
    files are safe to replace.
    """
    marker = target / MANIFEST_FILENAME
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("managed_by") != _MANIFEST_MANAGED_BY:
        return None
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    filenames: list[str] = []
    for entry in artifacts:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return None
        filenames.append(entry["path"])
    return filenames


def validate_save_target(target_dir: Path, *, overwrite: bool) -> Path:
    """Resolve ``target_dir`` and enforce the save-target invariants.

    Returns the resolved path. Raises ``ValueError`` when the path is not
    absolute, exists as a non-directory, or has no existing parent directory,
    and applies the marker-gated overwrite policy to a non-empty directory:
    it must contain a readable prior save manifest, ``overwrite`` must be
    True, and it must hold no files beyond that manifest's artifact list plus
    the marker itself. A new path or an empty directory passes without
    ``overwrite``. Runs before any subprocess, and again immediately before
    commit — the directory may have changed while the subprocess ran.
    """
    if not target_dir.is_absolute():
        raise ValueError(f"target_dir must be an absolute path: {target_dir}")
    target = target_dir.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError(f"target_dir exists but is not a directory: {target}")
    if not target.parent.is_dir():
        raise ValueError(f"target_dir parent directory does not exist: {target.parent}")
    if not target.is_dir():
        return target
    existing = sorted(entry.name for entry in target.iterdir())
    if not existing:
        return target
    tracked = _prior_manifest_filenames(target)
    if tracked is None:
        raise ValueError(
            f"refusing to write into {target}: the directory is not empty and does "
            f"not contain a readable prior save manifest ({MANIFEST_FILENAME}). "
            "Pick a new or empty directory."
        )
    if not overwrite:
        raise ValueError(
            f"refusing to overwrite the prior saved model at {target}; "
            "pass overwrite=true to replace it."
        )
    untracked = [name for name in existing if name != MANIFEST_FILENAME and name not in tracked]
    if untracked:
        raise ValueError(
            f"refusing to overwrite {target}: it contains files the prior save did "
            f"not write: {', '.join(untracked)}. Move them out or pick another directory."
        )
    return target


def _commit_staging(staging: Path, target: Path, backup: Path) -> str | None:
    """Swap ``staging`` into ``target``; return a cleanup warning or ``None``.

    A new target is one atomic rename. An existing (already re-gated) target
    is replaced wholesale: rename it to the backup sibling, rename staging
    into place, then remove the backup — restoring the backup when the swap
    itself fails, mirroring the installer's ``_swap_staging_into_place``.
    Whole-directory replacement is deliberate: it cannot leave stale artifacts
    behind when the new save omits a file the prior save wrote. On any
    failure, staging is removed and the original target is left (or restored)
    in place. A backup that survives a successful swap is reported as a
    warning string rather than failing the already-completed save.
    """
    if not target.exists():
        staging.rename(target)
        return None
    moved_aside = False
    target_swapped = False
    try:
        target.rename(backup)
        moved_aside = True
        staging.rename(target)
        target_swapped = True
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if moved_aside and not target_swapped and backup.exists():
            try:
                backup.rename(target)
            except OSError as restore_exc:
                raise ValueError(
                    f"save failed and the prior directory could not be restored from "
                    f"{backup}. Recover manually with `mv {backup} {target}`."
                ) from restore_exc
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as cleanup_exc:
            return (
                f"the replaced directory's backup at {backup} could not be removed "
                f"({cleanup_exc}); remove it manually with `rm -rf {backup}`."
            )
    return None


def commit_staged_dir(
    target: Path,
    *,
    overwrite: bool,
    write_files: Callable[[Path], list[SavedModelArtifact]],
) -> tuple[list[SavedModelArtifact], str | None]:
    """Stage, re-gate, and atomically commit a directory at ``target``.

    ``target`` must already be the resolved path ``validate_save_target``
    returned; the gate is re-run here immediately before commit because the
    directory may have changed while the solver ran. ``write_files`` is called
    with the staging directory and must write all artifact files (including the
    manifest) into it, returning the artifact list. Staging is a hidden sibling
    of ``target`` (same parent, so every rename stays on one filesystem). Any
    failure removes staging and leaves the prior target unchanged. Returns the
    saved artifact list and an optional post-commit cleanup warning.
    """
    token = uuid.uuid4().hex
    staging = target.parent / f".{target.name}.staging-{token}"
    backup = target.parent / f".{target.name}.backup-{token}"
    staging.mkdir()
    try:
        artifacts = write_files(staging)
        validate_save_target(target, overwrite=overwrite)
        warning = _commit_staging(staging, target, backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return artifacts, warning
