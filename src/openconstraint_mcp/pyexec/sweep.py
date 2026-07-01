"""CP-SAT Python seed-sweep orchestrator.

Runs one client-supplied CP-SAT script multiple times under different solver
seeds (via the ``OPENCONSTRAINT_MCP_CPSAT_SEED`` env protocol), applies the same
base-acceptance + optional-checker gates the save path uses, and selects the best
accepted incumbent for the requested objective sense.

This is deterministic orchestration over local child processes: it spawns no
processes itself (``run_cpsat_python``/``run_checker`` do), makes no network
calls, and runs attempts serially. The synchronous tool is bounded by a
pre-flight wall-clock *budget* (admission gate), not a runtime timer.

Imports only the dependency-light leaves (``childproc``, ``proc``, ``save_target``,
``schemas``) and the pyexec siblings ``core``/``checker``; never ``minizinc`` or
``runtime``.
"""

from __future__ import annotations

import math
import time
from typing import cast

from ..childproc import ChildProcessTracker
from ..proc import process_tree_terminate_worst_case_ms
from ..save_target import text_sha256
from ..schemas import (
    CpsatObjectiveSense,
    CpsatPythonResult,
    CpsatPythonSweepAttempt,
    CpsatPythonSweepResult,
    CpsatSweepStatus,
)
from .checker import run_checker
from .core import (
    CPSAT_SEED_ENV_VAR,
    DEFAULT_PYEXEC_TIMEOUT_MS,
    effective_checker_timeout_ms,
    run_cpsat_python,
    validate_checker_args,
    validate_cpsat_random_seed,
)

# The synchronous sweep's pre-flight wall-clock budget. A sweep whose projected
# budget exceeds this is rejected before any child runs, keeping the tool inside a
# typical synchronous MCP client timeout (2 min is comfortably under one).
MAX_SWEEP_WALL_CLOCK_MS: int = 120_000

# Small sweep-local allowance for the executor's polling interval and orchestration
# overhead, folded into each child's projected timeout overhead.
EXECUTOR_POLL_SLACK_MS: int = 250


def _child_timeout_overhead_ms() -> int:
    """Conservative per-child time a timed-out child can burn after the nominal cap.

    The process-tree leaf owns the termination sequence. The sweep adds only the
    executor's poll/orchestration slack on top of that worst-case cleanup budget.
    """
    return process_tree_terminate_worst_case_ms() + EXECUTOR_POLL_SLACK_MS


# Secondary sanity cap on the seed count, independent of per-request timeout. Eight
# seeds gives the caller useful breadth for small models while the wall-clock budget
# gate still rejects requests whose per-run/checker timeouts are too large.
MAX_SWEEP_SEEDS: int = 8

# Statuses a sweep treats as a usable (base-eligible) incumbent. ``timeout`` is a
# recovered partial — reportable, not savable; the save path's stricter reported
# gate ({optimal, feasible}) still rejects it.
_BASE_ACCEPT_STATUSES: frozenset[str] = frozenset({"optimal", "feasible", "timeout"})

# Stronger status wins a tie at equal objective; lower rank is better.
_STATUS_RANK: dict[str, int] = {"optimal": 0, "feasible": 1, "timeout": 2}


def _base_eligibility(result: CpsatPythonResult) -> tuple[bool, str | None]:
    """Return ``(eligible, reject_reason)``; ``reject_reason`` is set iff not eligible.

    Single source of truth for base acceptance, so the accept/reject verdict and
    the displayed rejection reason can never disagree about which condition failed.
    """
    if result.status not in _BASE_ACCEPT_STATUSES:
        return False, f"status={result.status!r}"
    if not result.solution:
        return False, "solution is missing or empty"
    if result.objective is None:
        return False, "objective is missing or non-numeric"
    return True, None


def _validate_objective_sense(objective_sense: object) -> CpsatObjectiveSense:
    if objective_sense not in ("maximize", "minimize"):
        raise ValueError("objective_sense must be 'maximize' or 'minimize'")
    return cast("CpsatObjectiveSense", objective_sense)


def _validate_sweep_request(
    *,
    seeds: list[int],
    objective_sense: object,
    per_run_timeout_ms: int,
    checker: str | None,
    checker_timeout_ms: int | None,
) -> tuple[CpsatObjectiveSense, int]:
    """Validate the request and return normalized sense + checker timeout.

    Raises ``ValueError`` (naming the limit and the offending value where a bound
    is involved) before any child is spawned.
    """
    validated_objective_sense = _validate_objective_sense(objective_sense)
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if per_run_timeout_ms <= 0:
        raise ValueError("per_run_timeout_ms must be positive")

    if not seeds:
        raise ValueError("seeds must not be empty")
    for index, seed in enumerate(seeds):
        validate_cpsat_random_seed(seed, label=f"seeds[{index}]")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must not contain duplicates")
    if len(seeds) > MAX_SWEEP_SEEDS:
        raise ValueError(
            f"seeds has {len(seeds)} entries, exceeding MAX_SWEEP_SEEDS={MAX_SWEEP_SEEDS}"
        )

    effective_checker_timeout = effective_checker_timeout_ms(
        checker_timeout_ms=checker_timeout_ms,
        default_timeout_ms=per_run_timeout_ms,
    )
    overhead = _child_timeout_overhead_ms()
    per_attempt = per_run_timeout_ms + overhead
    if checker is not None:
        per_attempt += effective_checker_timeout + overhead
    projected_ms = len(seeds) * per_attempt
    if projected_ms > MAX_SWEEP_WALL_CLOCK_MS:
        raise ValueError(
            f"projected sweep budget {projected_ms} ms exceeds "
            f"MAX_SWEEP_WALL_CLOCK_MS={MAX_SWEEP_WALL_CLOCK_MS} ms "
            "(reduce seed count or per_run_timeout_ms)"
        )
    return validated_objective_sense, effective_checker_timeout


def _solution_values_equal(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _solution_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _solution_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _same_incumbent(left: CpsatPythonResult, right: CpsatPythonResult) -> bool:
    return left.objective == right.objective and _solution_values_equal(
        left.solution, right.solution
    )


_COMPLETED_STATUSES: frozenset[str] = frozenset({"optimal", "feasible"})


def _seed_variation_hint(accepted: list[tuple[int, CpsatPythonResult]]) -> str | None:
    """Warn when accepted runs that completed (optimal/feasible) look seed-invariant.

    Restricted to completed runs: a ``timeout`` attempt is a partial search that can
    legitimately differ from another seed's result for reasons unrelated to seed
    handling, so it would be weak (and potentially misleading) evidence either way.
    """
    completed = [
        (index, result) for index, result in accepted if result.status in _COMPLETED_STATUSES
    ]
    if len(completed) < 2:
        return None

    first = completed[0][1]
    if all(_same_incumbent(first, result) for _, result in completed[1:]):
        return (
            "Accepted attempts that ran to completion (optimal/feasible) produced the "
            "same objective and solution; if you expected seed diversity, confirm the "
            f"script reads {CPSAT_SEED_ENV_VAR} and sets solver.parameters.random_seed."
        )
    return None


def _build_sweep_result(
    *,
    seeds: list[int],
    attempts: list[CpsatPythonSweepAttempt],
    elapsed_ms: int,
    objective_sense: CpsatObjectiveSense,
    distinct_accepted_objectives: int,
    seed_variation_hint: str | None,
    winner: tuple[int, CpsatPythonResult] | None,
    source_sha256: str,
    per_run_timeout_ms: int,
    checker_sha256: str | None,
    problem_sha256: str | None,
) -> CpsatPythonSweepResult:
    status: CpsatSweepStatus = "no_winner"
    winner_index: int | None = None
    winner_seed: int | None = None
    winner_result: CpsatPythonResult | None = None
    if winner is not None:
        status = "winner"
        winner_index, winner_result = winner
        winner_seed = seeds[winner_index]

    return CpsatPythonSweepResult(
        status=status,
        winner_index=winner_index,
        winner_seed=winner_seed,
        winner=winner_result,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        objective_sense=objective_sense,
        selection_policy="best_objective_then_status_then_seed",
        distinct_accepted_objectives=distinct_accepted_objectives,
        seed_variation_hint=seed_variation_hint,
        source_sha256=source_sha256,
        per_run_timeout_ms=per_run_timeout_ms,
        checker_sha256=checker_sha256,
        problem_sha256=problem_sha256,
    )


def run_cpsat_python_sweep(
    source: str,
    *,
    seeds: list[int],
    objective_sense: CpsatObjectiveSense,
    per_run_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    problem: str | None = None,
    checker: str | None = None,
    checker_timeout_ms: int | None = None,
    tracker: ChildProcessTracker | None = None,
) -> CpsatPythonSweepResult:
    """Run ``source`` once per seed and return the best accepted incumbent.

    Each attempt runs the same ``source`` with ``OPENCONSTRAINT_MCP_CPSAT_SEED``
    set to the seed; the script must read it and assign
    ``solver.parameters.random_seed`` (the server cannot force a seed into
    arbitrary Python). Attempts run SERIALLY in caller order.

    Acceptance is two ordered gates (short-circuiting like the save path): base
    acceptance (status ∈ {optimal, feasible, timeout}, non-empty solution, finite
    numeric objective), then — only for base-eligible attempts — the optional
    checker gate (accepted iff the checker returns ``accepted``). The checker is
    never spent on an attempt that already failed base acceptance.

    The winner is the accepted attempt with the best objective for
    ``objective_sense``, breaking ties by stronger status (optimal > feasible >
    timeout) then earliest provided seed order. Returns a ``CpsatPythonSweepResult`` with
    ``status="winner"`` and the winning ``CpsatPythonResult``/index/seed, or
    ``status="no_winner"`` when nothing was accepted. A ``timeout`` winner is a
    reportable best incumbent, not a savable one (it fails the save reported gate).

    Raises ``ValueError`` for an invalid request — including a projected budget over
    ``MAX_SWEEP_WALL_CLOCK_MS`` — before any child is spawned.

    The returned result's ``source_sha256``/``checker_sha256``/``problem_sha256``
    are the sha256 hex digests of the exact ``source``/``checker``/``problem`` text
    this call ran against (provenance only — see ``CpsatPythonSweepResult``).
    """
    validated_objective_sense, effective_checker_timeout = _validate_sweep_request(
        seeds=seeds,
        objective_sense=objective_sense,
        per_run_timeout_ms=per_run_timeout_ms,
        checker=checker,
        checker_timeout_ms=checker_timeout_ms,
    )
    source_sha256 = text_sha256(source)
    checker_sha256 = text_sha256(checker) if checker is not None else None
    problem_sha256 = text_sha256(problem) if problem is not None else None

    start = time.monotonic()
    attempts: list[CpsatPythonSweepAttempt] = []
    accepted: list[tuple[int, CpsatPythonResult]] = []

    for index, seed in enumerate(seeds):
        result = run_cpsat_python(
            source,
            timeout_ms=per_run_timeout_ms,
            tracker=tracker,
            env={CPSAT_SEED_ENV_VAR: str(seed)},
        )
        base_eligible, base_reject_reason = _base_eligibility(result)

        is_accepted = False
        checker_status = None
        message: str | None = base_reject_reason
        if base_eligible and checker is not None:
            report = run_checker(
                checker=checker,
                run_result=result,
                problem=problem,
                timeout_ms=effective_checker_timeout,
                tracker=tracker,
            )
            checker_status = report.status
            if report.status == "accepted":
                is_accepted = True
            else:
                message = f"checker {report.status}"
        elif base_eligible:
            is_accepted = True

        attempts.append(
            CpsatPythonSweepAttempt(
                index=index,
                seed=seed,
                status=result.status,
                objective=result.objective,
                accepted=is_accepted,
                checker_status=checker_status,
                message=message,
                timed_out=result.timed_out,
                truncated=result.truncated,
                duration_ms=result.duration_ms,
            )
        )
        if is_accepted:
            accepted.append((index, result))

    distinct_accepted_objectives = len({result.objective for _, result in accepted})
    seed_variation_hint = _seed_variation_hint(accepted)
    elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
    winner = (
        min(accepted, key=lambda item: _winner_sort_key(item, validated_objective_sense))
        if accepted
        else None
    )

    return _build_sweep_result(
        seeds=seeds,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        objective_sense=validated_objective_sense,
        distinct_accepted_objectives=distinct_accepted_objectives,
        seed_variation_hint=seed_variation_hint,
        winner=winner,
        source_sha256=source_sha256,
        per_run_timeout_ms=per_run_timeout_ms,
        checker_sha256=checker_sha256,
        problem_sha256=problem_sha256,
    )


def _winner_sort_key(
    item: tuple[int, CpsatPythonResult], objective_sense: CpsatObjectiveSense
) -> tuple[float, int, int]:
    """Sort key for accepted candidates; the minimum is the winner (lower is better).

    Base acceptance guarantees ``objective`` is a finite number, so negating it for
    ``maximize`` is safe. Ties break by stronger status, then earliest provided seed order.
    """
    index, result = item
    objective = result.objective
    assert objective is not None  # guaranteed by base acceptance
    objective_key = -objective if objective_sense == "maximize" else objective
    return (objective_key, _STATUS_RANK[result.status], index)
