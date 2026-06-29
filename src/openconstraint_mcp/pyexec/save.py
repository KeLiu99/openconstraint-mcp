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

from ..childproc import ChildProcessTracker
from ..save_target import (
    MANIFEST_FILENAME,
    commit_staged_dir,
    validate_save_target,
)
from ..save_target import tool_version as _tool_version
from ..schemas import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonResult,
    CpsatVerificationLevel,
    SavedArtifactRole,
    SavedModelArtifact,
    SaveVerifiedPythonResult,
)
from .checker import run_checker
from .core import (
    CPSAT_SEED_ENV_VAR,
    DEFAULT_PYEXEC_TIMEOUT_MS,
    VERIFIED_STATUSES,
    effective_checker_timeout_ms,
    run_cpsat_python,
    validate_checker_args,
    validate_cpsat_random_seed,
)

SCRIPT_FILENAME: str = "solution.py"
PROBLEM_FILENAME: str = "problem.txt"
CHECKER_FILENAME: str = "checker.py"
SOLUTION_FILENAME: str = "solution.json"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expectation_passes(
    run_result: CpsatPythonResult, expectation: CpsatExpectation
) -> tuple[bool, str | None]:
    """Check the objective threshold. Returns (passed, reason_if_failed)."""
    obj = run_result.objective
    if obj is None:
        return False, "expectation requires a numeric objective but the script emitted none"
    threshold = expectation.objective_threshold
    if expectation.objective_sense == "maximize":
        if obj >= threshold:
            return True, None
        return False, f"objective {obj} < threshold {threshold} (maximize)"
    else:
        if obj <= threshold:
            return True, None
        return False, f"objective {obj} > threshold {threshold} (minimize)"


def _failure(
    run_result: CpsatPythonResult,
    *,
    reason: str,
    verification_level: CpsatVerificationLevel,
    reported_passed: bool,
    expectation: CpsatExpectation | None,
    expectation_passed: bool | None,
    checker: CpsatCheckerReport | None,
) -> SaveVerifiedPythonResult:
    return SaveVerifiedPythonResult(
        status=run_result.status,
        target_dir=None,
        reason=reason,
        solution=run_result.solution,
        objective=run_result.objective,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
        timed_out=run_result.timed_out,
        truncated=run_result.truncated,
        duration_ms=run_result.duration_ms,
        verification_level=verification_level,
        reported_passed=reported_passed,
        expectation=expectation,
        expectation_passed=expectation_passed,
        checker=checker,
    )


def _write_staged_artifacts(
    staging: Path,
    *,
    source: str,
    problem: str | None,
    checker: str | None,
    run_result: CpsatPythonResult,
    verification_level: CpsatVerificationLevel,
    expectation: CpsatExpectation | None,
    expectation_passed: bool | None,
    checker_report: CpsatCheckerReport | None,
    seed: int | None,
) -> list[SavedModelArtifact]:
    texts: list[tuple[SavedArtifactRole, str, str]] = [("model", SCRIPT_FILENAME, source)]
    if problem is not None:
        texts.append(("problem", PROBLEM_FILENAME, problem))
    if checker is not None:
        texts.append(("checker", CHECKER_FILENAME, checker))
    if checker_report is not None:
        texts.append(
            (
                "solution",
                SOLUTION_FILENAME,
                json.dumps(run_result.solution or {}, indent=2) + "\n",
            )
        )

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        file_path.write_text(text, encoding="utf-8")
        artifacts.append(SavedModelArtifact(role=role, path=filename, sha256=_sha256_of(file_path)))

    verification: dict = {
        "level": verification_level,
        "reported_status": run_result.status,
        "objective": run_result.objective,
    }
    if seed is not None:
        # The saved solution.py is byte-for-byte the client's script: the seed lives
        # in the manifest, not the code. A manual re-run without the env var hits the
        # script's own fallback and may not reproduce this incumbent.
        verification["replay_seed"] = seed
        verification["reproducibility_note"] = (
            f"This result was verified with the replay seed above. The saved "
            f"{SCRIPT_FILENAME} carries its own seed fallback, not this seed; to "
            f"reproduce the saved incumbent, set {CPSAT_SEED_ENV_VAR}={seed} before "
            f"running {SCRIPT_FILENAME} by hand."
        )
    if expectation is not None:
        verification["expectation"] = {
            "objective_sense": expectation.objective_sense,
            "objective_threshold": expectation.objective_threshold,
            "passed": expectation_passed,
        }
    if checker_report is not None:
        # Only scalar summary — no stdout/stderr/errors/details to avoid leakage.
        verification["checker"] = {
            "status": checker_report.status,
            "error_count": len(checker_report.errors),
            "duration_ms": checker_report.duration_ms,
            "timed_out": checker_report.timed_out,
            "truncated": checker_report.truncated,
        }

    manifest = {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "verification": verification,
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
    expectation: CpsatExpectation | None = None,
    checker: str | None = None,
    checker_timeout_ms: int | None = None,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    overwrite: bool = False,
    seed: int | None = None,
    tracker: ChildProcessTracker | None = None,
) -> SaveVerifiedPythonResult:
    """Re-run ``source`` and persist it when it passes all supplied save gates.

    Gates run in order (reported → expectation → checker) and short-circuit on
    the first failure. Writing nothing on a non-passing run is guaranteed — the
    commit never starts. Returns a ``SaveVerifiedPythonResult`` with ``saved=True``
    and files on disk, or ``saved=False`` with a ``reason`` and the
    ``verification_level`` of the highest gate that actually passed.

    When ``seed`` is supplied (e.g. to persist a sweep's winning seed), the re-run
    sets ``OPENCONSTRAINT_MCP_CPSAT_SEED`` so a cooperating script replays that
    seed, and the manifest records it. The save gates are UNCHANGED: the reported
    gate still requires ``optimal``/``feasible``, so a ``timeout`` sweep winner is
    NOT savable even with its seed replayed — re-run it with a larger budget first.
    """
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if seed is not None:
        seed = validate_cpsat_random_seed(seed)

    effective_checker_timeout = effective_checker_timeout_ms(
        checker_timeout_ms=checker_timeout_ms,
        default_timeout_ms=timeout_ms,
    )
    seed_env = {CPSAT_SEED_ENV_VAR: str(seed)} if seed is not None else None

    target = validate_save_target(target_dir, overwrite=overwrite)
    run_result = run_cpsat_python(source, timeout_ms=timeout_ms, tracker=tracker, env=seed_env)

    # --- Reported gate ---
    reported_passed = run_result.status in VERIFIED_STATUSES and bool(run_result.solution)
    if not reported_passed:
        reason_parts = [f"status={run_result.status!r}"]
        if run_result.status in VERIFIED_STATUSES and not run_result.solution:
            reason_parts.append("solution is missing or empty")
        return _failure(
            run_result,
            reason=f"CP-SAT run did not pass the reported gate: {', '.join(reason_parts)}",
            verification_level="none",
            reported_passed=False,
            expectation=expectation,
            expectation_passed=None,
            checker=None,
        )

    # --- Expectation gate ---
    exp_passed: bool | None = None
    if expectation is not None:
        passed, exp_reason = _expectation_passes(run_result, expectation)
        exp_passed = passed
        if not passed:
            return _failure(
                run_result,
                reason=f"CP-SAT run did not pass the expectation gate: {exp_reason}",
                verification_level="reported",
                reported_passed=True,
                expectation=expectation,
                expectation_passed=False,
                checker=None,
            )

    # --- Checker gate ---
    checker_report: CpsatCheckerReport | None = None
    if checker is not None:
        _report: CpsatCheckerReport = run_checker(
            checker=checker,
            run_result=run_result,
            problem=problem,
            timeout_ms=effective_checker_timeout,
            tracker=tracker,
        )
        if _report.status != "accepted":
            return _failure(
                run_result,
                reason=(f"CP-SAT checker did not accept the solution: status={_report.status!r}"),
                verification_level="expectation" if expectation is not None else "reported",
                reported_passed=True,
                expectation=expectation,
                expectation_passed=exp_passed,
                checker=_report,
            )
        checker_report = _report

    # --- All gates passed: commit ---
    final_level: CpsatVerificationLevel
    if checker is not None:
        final_level = "checked"
    elif expectation is not None:
        final_level = "expectation"
    else:
        final_level = "reported"

    def _writer(staging: Path) -> list[SavedModelArtifact]:
        return _write_staged_artifacts(
            staging,
            source=source,
            problem=problem,
            checker=checker,
            run_result=run_result,
            verification_level=final_level,
            expectation=expectation,
            expectation_passed=exp_passed,
            checker_report=checker_report,
            seed=seed,
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
        verification_level=final_level,
        reported_passed=True,
        expectation=expectation,
        expectation_passed=exp_passed,
        checker=checker_report,
    )
