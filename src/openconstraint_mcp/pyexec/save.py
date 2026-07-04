"""CP-SAT Python artifact writer and save orchestrator.

The filesystem leaf behind ``save_verified_cpsat_python``. Generic save-target
policy (validation, manifest I/O, atomic commit) lives in ``save_target``;
this module supplies the CP-SAT-specific writer and the public function.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..childproc import ChildProcessTracker
from ..save_target import (
    EXPERIMENT_LOG_FILENAME,
    MANIFEST_FILENAME,
    commit_staged_dir,
    text_sha256,
    validate_save_target,
)
from ..save_target import tool_version as _tool_version
from ..schemas import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonExperimentResult,
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
    config_sha256,
    effective_checker_timeout_ms,
    run_cpsat_python,
    seed_config_env,
    validate_checker_args,
    validate_cpsat_random_seed,
    write_config_file,
)

SCRIPT_FILENAME: str = "solution.py"
PROBLEM_FILENAME: str = "problem.txt"
CHECKER_FILENAME: str = "checker.py"
SOLUTION_FILENAME: str = "solution.json"
REPLAY_CONFIG_FILENAME: str = "replay-config.json"


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


def _validate_experiment_result_consistency(
    experiment_result: CpsatPythonExperimentResult,
    *,
    source: str,
    seed: int | None,
    config: dict[str, Any] | None,
) -> None:
    """Eagerly reject an ``experiment_result`` that cannot describe this save request.

    Mirrors ``minizinc/core.py``'s ``_validate_portfolio_result_consistency``, minus
    the bounds recheck that function needs: unlike ``PortfolioSolveResult``,
    ``CpsatPythonExperimentResult``'s own model_validator already guarantees
    ``winner_index`` is in range whenever ``status == "winner"``, so this trusts
    that invariant instead of re-checking it (see the schema's docstring). This
    guards only against *accidental* mismatch (wrong script attached, a stale
    experiment, a seed/config that doesn't match the winning attempt) — it is not,
    and cannot be, a proof that ``experiment_result`` is honest. The save decision
    itself never reads ``experiment_result``; only the fresh re-run below and its
    gates decide whether this save succeeds. ``config`` here is already normalized
    (``{}`` -> ``None``) by the caller.
    """
    if experiment_result.status != "winner":
        raise ValueError(
            "experiment_result.status must be 'winner' to attach an experiment log "
            f"(got {experiment_result.status!r}); a no_winner experiment has nothing "
            "to attach"
        )
    winner_index = experiment_result.winner_index
    assert winner_index is not None  # guaranteed by status=="winner" (schema invariant)
    winning_attempt = experiment_result.attempts[winner_index]
    if winning_attempt.source_sha256 != text_sha256(source):
        raise ValueError(
            "experiment_result's winning attempt's source_sha256 does not match "
            "the supplied source: the experiment_result was attached to a different script"
        )
    if winning_attempt.seed != seed:
        raise ValueError(
            f"experiment_result's winning attempt's seed ({winning_attempt.seed!r}) "
            f"does not match the supplied save seed ({seed!r})"
        )
    expected_config_sha256 = config_sha256(config)
    if winning_attempt.config_sha256 != expected_config_sha256:
        raise ValueError(
            "experiment_result's winning attempt's config_sha256 "
            f"({winning_attempt.config_sha256!r}) does not match the canonical hash of "
            f"the supplied save config ({expected_config_sha256!r}); the save must "
            "replay the winning attempt's config exactly — this rejects both a save "
            "that supplies a config the winner ran without, and one that omits a "
            "config the winner used"
        )


def _build_experiment_log(experiment_result: CpsatPythonExperimentResult) -> dict[str, Any]:
    """Build the ``experiment-log.json`` content for a ``CpsatPythonExperimentResult``.

    A provenance SUMMARY, not an archive: every attempt row carries only hashes
    and scalar outcomes — never a non-winning attempt's full ``config`` object.
    The winning attempt's replay config is persisted separately as
    ``replay-config.json`` (see ``_write_staged_artifacts``).
    """
    return {
        "managed_by": "openconstraint-mcp",
        "tool_version": _tool_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "exploration_type": "cpsat_python_experiment",
        "objective_sense": experiment_result.objective_sense,
        "selection_policy": experiment_result.selection_policy,
        "winner_index": experiment_result.winner_index,
        "winner_name": experiment_result.winner_name,
        "source_sha256": experiment_result.source_sha256,
        "checker_sha256": experiment_result.checker_sha256,
        "problem_sha256": experiment_result.problem_sha256,
        "elapsed_ms": experiment_result.elapsed_ms,
        "attempts": [
            {
                "index": attempt.index,
                "name": attempt.name,
                "seed": attempt.seed,
                "source_sha256": attempt.source_sha256,
                "config_sha256": attempt.config_sha256,
                "timeout_ms": attempt.timeout_ms,
                "status": attempt.status,
                "objective": attempt.objective,
                "accepted": attempt.accepted,
                "checker_status": attempt.checker_status,
                "message": attempt.message,
                "timed_out": attempt.timed_out,
                "truncated": attempt.truncated,
                "duration_ms": attempt.duration_ms,
            }
            for attempt in experiment_result.attempts
        ],
    }


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
    config: dict[str, Any] | None,
    experiment_result: CpsatPythonExperimentResult | None,
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
    if config is not None:
        # The winning attempt's replay config, persisted for auditability and
        # best-effort replay — never a non-winning attempt's config (see
        # _build_experiment_log).
        texts.append(("replay_config", REPLAY_CONFIG_FILENAME, json.dumps(config, indent=2) + "\n"))
    if experiment_result is not None:
        texts.append(
            (
                "experiment_log",
                EXPERIMENT_LOG_FILENAME,
                json.dumps(_build_experiment_log(experiment_result), indent=2) + "\n",
            )
        )

    artifacts: list[SavedModelArtifact] = []
    for role, filename, text in texts:
        file_path = staging / filename
        file_path.write_text(text, encoding="utf-8")
        artifacts.append(SavedModelArtifact(role=role, path=filename, sha256=_sha256_of(file_path)))

    verification: dict[str, Any] = {
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
    if config is not None:
        verification["replay_config_sha256"] = config_sha256(config)
    if experiment_result is not None:
        # Compact summary only — the full attempt table lives in
        # experiment-log.json, not duplicated here, so the manifest stays skimmable.
        verification["experiment_log"] = {
            "exploration_type": "cpsat_python_experiment",
            "winner_index": experiment_result.winner_index,
            "winner_name": experiment_result.winner_name,
            "attempt_count": len(experiment_result.attempts),
            "accepted_attempt_count": sum(1 for a in experiment_result.attempts if a.accepted),
            "statuses_seen": sorted({a.status for a in experiment_result.attempts}),
            "selection_policy": experiment_result.selection_policy,
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
    config: dict[str, Any] | None = None,
    experiment_result: CpsatPythonExperimentResult | None = None,
    tracker: ChildProcessTracker | None = None,
) -> SaveVerifiedPythonResult:
    """Re-run ``source`` and persist it when it passes all supplied save gates.

    Gates run in order (reported → expectation → checker) and short-circuit on
    the first failure. Writing nothing on a non-passing run is guaranteed — the
    commit never starts. Returns a ``SaveVerifiedPythonResult`` with ``saved=True``
    and files on disk, or ``saved=False`` with a ``reason`` and the
    ``verification_level`` of the highest gate that actually passed.

    When ``seed`` is supplied, the re-run sets ``OPENCONSTRAINT_MCP_CPSAT_SEED`` so
    a cooperating script replays that seed, and the manifest records it. This is a
    single-run replay aid only: the save gates are UNCHANGED, and the reported gate
    still requires ``optimal``/``feasible``.

    ``config`` is likewise a replay aid: a non-empty dict is written to a temp file
    and its path injected via ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` for the re-run,
    then persisted as ``replay-config.json`` on a successful save (``{}`` is
    normalized to "no config", identically to the experiment executor).

    ``experiment_result`` is PROVENANCE ONLY, never verification evidence — like
    ``portfolio_result`` on the MiniZinc save path. When supplied, it is validated
    eagerly for self-consistency with this request (winner status, matching
    ``source``/``seed``/``config`` hashes — see
    ``_validate_experiment_result_consistency``) but every save decision still
    comes from the fresh run/gates below. On a successful save, its attempt table
    is copied into ``experiment-log.json`` as a provenance summary (hashes and
    scalar outcomes only, never a non-winning attempt's full ``config``).
    """
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if seed is not None:
        seed = validate_cpsat_random_seed(seed)
    normalized_config = config if config else None
    if experiment_result is not None:
        _validate_experiment_result_consistency(
            experiment_result,
            source=source,
            seed=seed,
            config=normalized_config,
        )

    effective_checker_timeout = effective_checker_timeout_ms(
        checker_timeout_ms=checker_timeout_ms,
        default_timeout_ms=timeout_ms,
    )

    target = validate_save_target(target_dir, overwrite=overwrite)
    if normalized_config is not None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config_file(Path(tmp_dir), normalized_config)
            replay_env = seed_config_env(seed=seed, config_path=config_path)
            run_result = run_cpsat_python(
                source, timeout_ms=timeout_ms, tracker=tracker, env=replay_env
            )
    else:
        replay_env = seed_config_env(seed=seed, config_path=None)
        run_result = run_cpsat_python(
            source, timeout_ms=timeout_ms, tracker=tracker, env=replay_env
        )

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
            config=normalized_config,
            experiment_result=experiment_result,
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
