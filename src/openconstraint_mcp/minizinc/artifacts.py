"""Staging, hashing, manifest, and commit logic for saved verified models.

The filesystem leaf behind ``core.save_verified_model``: it validates the
user-supplied target directory, stages the artifact files beside it, writes
the manifest marker, and commits the staged directory with the same
backup-swap posture as the runtime installer's ``_swap_staging_into_place``
(re-implemented here — ``runtime_install`` is a CLI-only leaf this layer must
not import). It never runs MiniZinc and never decides *whether* to save; the
verification gate lives in ``core``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from ..schemas import CheckResult, SavedArtifactRole, SavedModelArtifact, SolveResult

# The fixed artifact layout of a saved verified-model directory. Filenames are
# part of the user-facing contract (stable, never LLM-controlled), which is what
# keeps path validation small: the only user-chosen path is the directory.
MODEL_FILENAME: str = "model.mzn"
DATA_FILENAME: str = "data.dzn"
CHECKER_FILENAME: str = "checker.mzc.mzn"
PROBLEM_FILENAME: str = "problem.md"
SOLVE_RESULT_FILENAME: str = "solve-result.json"
# The manifest doubles as the managed-directory marker: only a directory whose
# marker parses (see _prior_manifest_filenames) may ever be overwritten.
MANIFEST_FILENAME: str = ".openconstraint-model.json"

_MANIFEST_MANAGED_BY: str = "openconstraint-mcp"
_PACKAGE_NAME: str = "openconstraint-mcp"


def _tool_version() -> str:
    """Return the installed package version, or ``"unknown"`` if unavailable."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def _sha256_of(path: Path) -> str:
    """Hash the file's bytes as written to disk (post-write, not pre-write)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        # ValueError covers both JSONDecodeError and UnicodeDecodeError.
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
    commit — the directory may have changed while MiniZinc ran.
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


def _write_staged_artifacts(
    staging: Path,
    *,
    model: str,
    data: str | None,
    checker: str | None,
    problem: str | None,
    check: CheckResult,
    solve: SolveResult,
    solve_controls: dict[str, Any],
) -> list[SavedModelArtifact]:
    """Write every artifact into ``staging`` and return the saved-file list.

    Text artifacts are written verbatim as UTF-8 and hashed from disk after
    the write. The manifest is written last: it lists every *other* file (it
    cannot hash itself), then joins the returned list as a final entry hashed
    after its own write.
    """
    texts: list[tuple[SavedArtifactRole, str, str]] = [("model", MODEL_FILENAME, model)]
    if data is not None:
        texts.append(("data", DATA_FILENAME, data))
    if checker is not None:
        texts.append(("checker", CHECKER_FILENAME, checker))
    if problem is not None:
        texts.append(("problem", PROBLEM_FILENAME, problem))
    texts.append(
        (
            "solve_result",
            SOLVE_RESULT_FILENAME,
            json.dumps(solve.model_dump(mode="json"), indent=2) + "\n",
        )
    )

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        file_path.write_text(text, encoding="utf-8")
        artifacts.append(SavedModelArtifact(role=role, path=filename, sha256=_sha256_of(file_path)))

    manifest = {
        "managed_by": _MANIFEST_MANAGED_BY,
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "solver": solve.solver,
        "solve_controls": solve_controls,
        "verification": {
            "check_status": check.status,
            "solve_status": solve.status,
            "checker_status": solve.checker.status if solve.checker is not None else None,
        },
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }
    manifest_path = staging / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    artifacts.append(
        SavedModelArtifact(
            role="manifest", path=MANIFEST_FILENAME, sha256=_sha256_of(manifest_path)
        )
    )
    return artifacts


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


def write_verified_model_dir(
    target: Path,
    *,
    model: str,
    data: str | None,
    checker: str | None,
    problem: str | None,
    check: CheckResult,
    solve: SolveResult,
    solve_controls: dict[str, Any],
    overwrite: bool,
) -> tuple[list[SavedModelArtifact], str | None]:
    """Stage, re-gate, and commit a verified-model directory at ``target``.

    ``target`` must already be the resolved path ``validate_save_target``
    returned; the gate is re-run here immediately before commit because the
    directory may have changed while MiniZinc ran. Staging is a hidden sibling
    of ``target`` (same parent, so every rename stays on one filesystem). Any
    failure removes staging and leaves the prior target unchanged. Returns the
    saved artifact list and an optional post-commit cleanup warning.
    """
    token = uuid.uuid4().hex
    staging = target.parent / f".{target.name}.staging-{token}"
    backup = target.parent / f".{target.name}.backup-{token}"
    staging.mkdir()
    try:
        artifacts = _write_staged_artifacts(
            staging,
            model=model,
            data=data,
            checker=checker,
            problem=problem,
            check=check,
            solve=solve,
            solve_controls=solve_controls,
        )
        validate_save_target(target, overwrite=overwrite)
        warning = _commit_staging(staging, target, backup)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return artifacts, warning
