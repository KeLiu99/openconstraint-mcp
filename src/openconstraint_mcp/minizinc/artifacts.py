"""MiniZinc-specific artifact layout and file-writer for saved verified models.

The filesystem leaf behind ``core.save_verified_model``. It never runs
MiniZinc and never decides *whether* to save; the verification gate lives in
``core``. Generic save-target policy (validation, manifest I/O, atomic commit)
lives in ``save_target``; this module supplies the MiniZinc-specific
``_write_staged_artifacts`` writer and the thin ``write_verified_model_dir``
wrapper.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..save_target import (
    MANIFEST_FILENAME,
    commit_staged_dir,
)
from ..save_target import (
    tool_version as _tool_version,
)
from ..schemas import CheckResult, SavedArtifactRole, SavedModelArtifact, SolveResult


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The fixed artifact layout of a saved verified-model directory. Filenames are
# part of the user-facing contract (stable, never LLM-controlled).
MODEL_FILENAME: str = "model.mzn"
DATA_FILENAME: str = "data.dzn"
CHECKER_FILENAME: str = "checker.mzc.mzn"
PROBLEM_FILENAME: str = "problem.md"
SOLVE_RESULT_FILENAME: str = "solve-result.json"


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
        "managed_by": "openconstraint-mcp",
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
    returned. Delegates the generic staging/commit to ``commit_staged_dir``
    from ``save_target``; supplies the MiniZinc-specific file writer.
    Returns the saved artifact list and an optional post-commit cleanup warning.
    """

    def _writer(staging: Path) -> list[SavedModelArtifact]:
        return _write_staged_artifacts(
            staging,
            model=model,
            data=data,
            checker=checker,
            problem=problem,
            check=check,
            solve=solve,
            solve_controls=solve_controls,
        )

    return commit_staged_dir(target, overwrite=overwrite, write_files=_writer)
