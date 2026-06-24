"""CP-SAT Python artifact writer and save orchestrator.

The filesystem leaf behind ``save_verified_cpsat_python``. Generic save-target
policy (validation, manifest I/O, atomic commit) lives in ``save_target``;
this module supplies the CP-SAT-specific writer and the public function.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, computed_field

from ..save_target import (
    MANIFEST_FILENAME,
    commit_staged_dir,
    validate_save_target,
)
from ..save_target import tool_version as _tool_version
from ..schemas import SavedArtifactRole, SavedModelArtifact
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    VERIFIED_STATUSES,
    CpsatPythonResult,
    CpsatStatus,
    run_cpsat_python,
)

SCRIPT_FILENAME: str = "solution.py"
PROBLEM_FILENAME: str = "problem.txt"


class SaveVerifiedPythonResult(BaseModel):
    status: CpsatStatus
    target_dir: str | None
    reason: str | None
    solution: dict | None
    objective: float | int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    files: list[SavedModelArtifact] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def saved(self) -> bool:
        return self.reason is None and self.target_dir is not None


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_staged_artifacts(
    staging: Path,
    *,
    source: str,
    problem: str | None,
    run_result: CpsatPythonResult,
) -> list[SavedModelArtifact]:
    texts: list[tuple[SavedArtifactRole, str, str]] = [("model", SCRIPT_FILENAME, source)]
    if problem is not None:
        texts.append(("problem", PROBLEM_FILENAME, problem))

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        file_path.write_text(text, encoding="utf-8")
        artifacts.append(SavedModelArtifact(role=role, path=filename, sha256=_sha256_of(file_path)))

    manifest = {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "verification": {
            "status": run_result.status,
            "objective": run_result.objective,
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


def save_verified_cpsat_python(
    source: str,
    *,
    target_dir: Path,
    problem: str | None = None,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    overwrite: bool = False,
) -> SaveVerifiedPythonResult:
    """Re-run ``source`` and persist it when it yields a verified solution.

    ``validate_save_target`` runs before the executor (fail-fast on bad paths)
    and again immediately before commit (inside ``commit_staged_dir``). Returns
    a ``SaveVerifiedPythonResult`` with ``saved=True`` and files on disk, or
    ``saved=False`` with a ``reason`` when the run does not qualify. Writing
    nothing on a non-verified run is guaranteed — the commit never starts.
    """
    target = validate_save_target(target_dir, overwrite=overwrite)
    run_result = run_cpsat_python(source, timeout_ms=timeout_ms)

    if run_result.status not in VERIFIED_STATUSES or not run_result.solution:
        reason_parts = [f"status={run_result.status!r}"]
        if not run_result.solution and run_result.status in VERIFIED_STATUSES:
            reason_parts.append("solution is missing or empty")
        return SaveVerifiedPythonResult(
            status=run_result.status,
            target_dir=None,
            reason=f"CP-SAT run did not yield a verified solution: {', '.join(reason_parts)}",
            solution=run_result.solution,
            objective=run_result.objective,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            timed_out=run_result.timed_out,
            truncated=run_result.truncated,
            duration_ms=run_result.duration_ms,
        )

    def _writer(staging: Path) -> list[SavedModelArtifact]:
        return _write_staged_artifacts(
            staging,
            source=source,
            problem=problem,
            run_result=run_result,
        )

    files, _ = commit_staged_dir(target, overwrite=overwrite, write_files=_writer)
    return SaveVerifiedPythonResult(
        status=run_result.status,
        target_dir=str(target),
        reason=None,
        solution=run_result.solution,
        objective=run_result.objective,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
        timed_out=run_result.timed_out,
        truncated=run_result.truncated,
        duration_ms=run_result.duration_ms,
        files=files,
    )
