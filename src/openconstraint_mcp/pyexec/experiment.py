"""CP-SAT Python explicit-experiment orchestrator.

Runs a client-supplied list of explicit attempts — each a complete, independent
CP-SAT Python script, optionally paired with a seed and/or a cooperative JSON
config — through the existing CP-SAT child runner and checker, and selects the
best accepted incumbent. This generalizes the (removed) seed sweep: instead of
one source run under N seeds, an experiment runs N independently-specified
attempts, each of which may vary source, seed, and/or config.

The server does not generate attempts, does not mutate OR-Tools objects, and
does not set solver parameters itself. It only writes a non-empty ``config`` to
a temp file and points ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` at it (and a seed via
``OPENCONSTRAINT_MCP_CPSAT_SEED``); a cooperating script decides how to apply
either.

Attempts run through a bounded ``ThreadPoolExecutor`` (``max_parallel_attempts``
workers, default 1 = serial); ``run_cpsat_python`` blocks on a subprocess wait,
so a worker thread spends nearly all its time off the GIL. Results are
assembled in original attempt order regardless of completion order, and winner
tie-breaks use that same original order — never completion order.

This is a synchronous, budget-gated tool, not a background job: a projected
worst-case wall-clock estimate is checked before any child runs (see
``_check_wall_clock_budget``), and the request is rejected outright when it
would exceed the budget.

Imports only the dependency-light leaves (``childproc``, ``proc``,
``save_target``, ``schemas``, ``eligibility``) and the pyexec siblings
``core``/``checker``; never ``minizinc`` or ``runtime``.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, cast

from ..schemas.cpsat import (
    CpsatExperimentSelectionPolicy,
    CpsatObjectiveSense,
    CpsatPythonExperimentAttempt,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from ..shared.childproc import ChildProcessTracker
from ..shared.proc import process_tree_terminate_worst_case_ms
from ..shared.save_target import text_sha256
from .checker import run_checker
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    canonical_json_byte_length,
    config_sha256,
    effective_checker_timeout_ms,
    run_cpsat_python,
    seed_config_env,
    validate_checker_args,
    validate_cpsat_random_seed,
    write_config_file,
)
from .eligibility import diagnostic_incumbent_eligibility

# The synchronous experiment's pre-flight wall-clock budget, matching the
# removed seed sweep's MAX_SWEEP_WALL_CLOCK_MS: comfortably under a typical
# synchronous MCP client timeout.
MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS: int = 120_000

# Small allowance for the executor's polling interval and orchestration
# overhead, folded into each attempt's projected timeout overhead.
EXECUTOR_POLL_SLACK_MS: int = 250

# A config dict is a small cooperative parameter bag, not a data payload — bound
# its canonical JSON encoding well under the 1 MiB child-output cap.
MAX_EXPERIMENT_CONFIG_BYTES: int = 64 * 1024

# Placeholder written into a suppressed winner's stdout when the caller opts
# out via include_winner_stdout=False. A fixed, recognizable sentinel (not an
# empty string) so a client can tell "the script printed nothing" apart from
# "the server omitted this by request".
_WINNER_STDOUT_OMITTED_SENTINEL: str = "<omitted: include_winner_stdout=False>"

# The hard ceiling on max_parallel_attempts, independent of any client-requested
# value: never oversubscribe the local machine by more than a handful of
# concurrent CP-SAT children (each of which may itself use multiple workers).
_MAX_PARALLEL_ATTEMPTS_CAP_LIMIT: int = 4

# Unconditional advisory attached to every winner: an experiment winner is one
# observed run, not a reproducibility guarantee. This is deliberately NOT
# gated on inspecting attempt.seed or config["num_workers"] — the server
# cannot see how (or whether) a script's source actually applies those, so a
# narrower "only warn when seed is unset" heuristic would be both brittle and
# falsely reassuring when it stays silent.
_REPRODUCIBILITY_WARNING: str = (
    "This winner reflects one observed run, not a reproducibility guarantee. "
    "CP-SAT's randomized search, LNS, restarts, parallel portfolio search "
    "(num_workers > 1), and short time limits can all produce a "
    "different objective on replay — save_verified_cpsat_python re-runs this "
    "script fresh and may find a worse (or better) result. For stronger "
    "reproducibility, set explicit solver parameters such as random_seed, "
    "consider num_workers = 1, and verify with the same timeout — but "
    "exact determinism is not guaranteed."
)

# Stronger status wins ties; lower rank is better.
_STATUS_RANK: dict[str, int] = {"optimal": 0, "feasible": 1, "timeout": 2}

_OPTIMIZATION_SELECTION_POLICY: CpsatExperimentSelectionPolicy = (
    "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
)
_FEASIBILITY_SELECTION_POLICY: CpsatExperimentSelectionPolicy = (
    "accepted_status_then_duration_then_attempt_order"
)


def _max_parallel_attempts_cap() -> int:
    return min(os.cpu_count() or 1, _MAX_PARALLEL_ATTEMPTS_CAP_LIMIT)


def _attempt_eligibility(
    result: CpsatPythonResult, objective_sense: CpsatObjectiveSense | None
) -> tuple[bool, str | None]:
    """Return ``(eligible, reject_reason)``; ``reject_reason`` is set iff not eligible.

    The status/solution gate is the shared ``eligibility`` leaf (also used by
    background jobs); the optimization-mode objective check layered on top is
    experiment-specific, so it stays here.
    """
    eligible, reject_reason = diagnostic_incumbent_eligibility(result)
    if not eligible:
        return False, reject_reason
    if objective_sense is None:
        return True, None
    objective = result.objective
    if objective is None or isinstance(objective, bool) or not math.isfinite(objective):
        return False, "objective is missing or non-numeric"
    return True, None


def _validate_objective_sense(objective_sense: object) -> CpsatObjectiveSense | None:
    if objective_sense is None:
        return None
    if objective_sense not in ("maximize", "minimize"):
        raise ValueError("objective_sense must be 'maximize', 'minimize', or None")
    return cast("CpsatObjectiveSense", objective_sense)


def _selection_policy(
    objective_sense: CpsatObjectiveSense | None,
) -> CpsatExperimentSelectionPolicy:
    if objective_sense is None:
        return _FEASIBILITY_SELECTION_POLICY
    return _OPTIMIZATION_SELECTION_POLICY


def _resolved_name(attempt: CpsatPythonExperimentAttempt, index: int) -> str:
    return attempt.name if attempt.name is not None else f"attempt-{index}"


def _validate_attempts(attempts: Sequence[CpsatPythonExperimentAttempt]) -> list[str]:
    """Validate attempts and return resolved display names, index-aligned.

    Raises ``ValueError`` for: an empty attempts list, an empty/whitespace-only
    source, an out-of-range seed, an oversized config, a non-positive
    ``timeout_ms``, or a name collision (explicit vs. explicit, or explicit vs.
    a defaulted ``attempt-{index}`` label).
    """
    if not attempts:
        raise ValueError("attempts must not be empty")

    names: list[str] = []
    seen: set[str] = set()
    for index, attempt in enumerate(attempts):
        name = _resolved_name(attempt, index)
        if name in seen:
            raise ValueError(f"duplicate attempt name (explicit or defaulted): {name!r}")
        seen.add(name)
        names.append(name)

        if not attempt.source.strip():
            raise ValueError(f"attempts[{index}].source must be non-empty")
        if attempt.seed is not None:
            validate_cpsat_random_seed(attempt.seed, label=f"attempts[{index}].seed")
        if attempt.config:
            size = canonical_json_byte_length(attempt.config)
            if size > MAX_EXPERIMENT_CONFIG_BYTES:
                raise ValueError(
                    f"attempts[{index}].config canonical JSON is {size} bytes, "
                    f"exceeding MAX_EXPERIMENT_CONFIG_BYTES={MAX_EXPERIMENT_CONFIG_BYTES}"
                )
        if attempt.timeout_ms is not None and attempt.timeout_ms <= 0:
            raise ValueError(f"attempts[{index}].timeout_ms must be positive")
    return names


def _validate_max_parallel_attempts(max_parallel_attempts: object) -> int:
    if isinstance(max_parallel_attempts, bool) or not isinstance(max_parallel_attempts, int):
        raise ValueError("max_parallel_attempts must be a non-bool positive integer")
    if max_parallel_attempts < 1:
        raise ValueError("max_parallel_attempts must be >= 1")
    cap = _max_parallel_attempts_cap()
    if max_parallel_attempts > cap:
        raise ValueError(
            f"max_parallel_attempts={max_parallel_attempts} exceeds the server cap "
            f"{cap} (= min(os.cpu_count(), {_MAX_PARALLEL_ATTEMPTS_CAP_LIMIT}))"
        )
    return max_parallel_attempts


def _effective_timeout_ms(attempt: CpsatPythonExperimentAttempt, default_timeout_ms: int) -> int:
    return attempt.timeout_ms if attempt.timeout_ms is not None else default_timeout_ms


def _child_timeout_overhead_ms() -> int:
    """Conservative per-child time a timed-out child can burn after its nominal cap.

    The process-tree leaf owns the termination sequence. The experiment adds
    only the executor's poll/orchestration slack on top of that worst-case
    cleanup budget.
    """
    return process_tree_terminate_worst_case_ms() + EXECUTOR_POLL_SLACK_MS


class _AttemptBudget(NamedTuple):
    """One attempt's admission-budget components, in ms.

    ``checker_timeout_ms``/``checker_budget_ms`` are ``None``/``0`` when the
    experiment has no checker — there is nothing to charge a checker budget for.
    """

    timeout_ms: int
    checker_timeout_ms: int | None
    attempt_budget_ms: int
    checker_budget_ms: int
    total_ms: int


def _attempt_budget_breakdown(
    attempt: CpsatPythonExperimentAttempt,
    *,
    default_timeout_ms: int,
    checker_present: bool,
    checker_timeout_ms: int | None,
) -> _AttemptBudget:
    """Break one attempt's projected worst-case wall-clock time into its components.

    Single source of truth for the admission-budget math, so the pass/fail gate
    (``_check_wall_clock_budget``) and its rejection message can never disagree
    about how a projected total was derived.
    """
    overhead = _child_timeout_overhead_ms()
    timeout_ms = _effective_timeout_ms(attempt, default_timeout_ms)
    attempt_budget_ms = timeout_ms + overhead
    effective_checker_ms: int | None = None
    checker_budget_ms = 0
    if checker_present:
        effective_checker_ms = effective_checker_timeout_ms(
            checker_timeout_ms=checker_timeout_ms, default_timeout_ms=timeout_ms
        )
        checker_budget_ms = effective_checker_ms + overhead
    return _AttemptBudget(
        timeout_ms=timeout_ms,
        checker_timeout_ms=effective_checker_ms,
        attempt_budget_ms=attempt_budget_ms,
        checker_budget_ms=checker_budget_ms,
        total_ms=attempt_budget_ms + checker_budget_ms,
    )


def _check_wall_clock_budget(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    *,
    default_timeout_ms: int,
    max_parallel_attempts: int,
    checker_present: bool,
    checker_timeout_ms: int | None,
) -> None:
    """Reject a projected over-budget request before any child runs.

    Batches attempts by ``max_parallel_attempts``: the projection is
    ``ceil(len(attempts) / max_parallel_attempts) * worst_case_of_the_slowest_attempt``,
    a conservative upper bound on wall-clock time for a bounded thread-pool
    schedule (parallelism can only reduce the true wall-clock time relative to
    this bound, never exceed it). On rejection, the error breaks the total down
    by the slowest attempt's own components so a caller can see whether the
    culprit is attempt count, per-attempt timeout, or the checker timeout —
    instead of only a single opaque "over budget" total. When batching (not a
    single attempt alone) is the culprit, the hint also names concrete
    single-lever fixes (a max attempt count, a min ``max_parallel_attempts``,
    or a max per-attempt total) derived from the same breakdown, so a caller
    does not have to invert the budget formula by hand.
    """
    breakdowns = [
        _attempt_budget_breakdown(
            attempt,
            default_timeout_ms=default_timeout_ms,
            checker_present=checker_present,
            checker_timeout_ms=checker_timeout_ms,
        )
        for attempt in attempts
    ]
    batches = math.ceil(len(attempts) / max_parallel_attempts)
    slowest = max(breakdowns, key=lambda b: b.total_ms)
    projected_ms = batches * slowest.total_ms
    if projected_ms <= MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS:
        return

    if slowest.total_ms > MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS:
        # Even a single run of the slowest attempt (batches=1) is already over
        # the cap: no attempt-count or max_parallel_attempts change can fit it.
        hint = (
            "the slowest attempt alone already exceeds the cap, so reducing "
            "attempt count or raising max_parallel_attempts cannot fit it — for "
            "a single attempt near or over this cap, use run_cpsat_python "
            "instead of run_cpsat_python_experiment"
        )
    else:
        # Each lever is solved holding the other two fixed, from the same
        # batches/slowest values the projection above already used:
        #   - batches_max: the most batches of the slowest attempt that fit
        #     the cap; every other bound follows from it.
        #   - max_attempts_to_fit: batches_max * max_parallel_attempts (the
        #     largest attempt count admitted at today's parallelism).
        #   - min_parallel_to_fit: ceil(attempt_count / batches_max) (the
        #     least parallelism that admits today's attempt count), flagged
        #     when it exceeds this machine's own parallelism cap.
        #   - max_slowest_total_ms: MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS //
        #     batches (today's batches, not batches_max) — the ceiling the
        #     slowest attempt's timeout_ms + overhead + checker budget must
        #     drop under at today's attempt count and parallelism.
        batches_max = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // slowest.total_ms
        max_attempts_to_fit = batches_max * max_parallel_attempts
        min_parallel_to_fit = math.ceil(len(attempts) / batches_max)
        max_slowest_total_ms = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // batches
        parallel_cap = _max_parallel_attempts_cap()
        parallel_note = (
            f" (exceeds this machine's max_parallel_attempts cap of {parallel_cap})"
            if min_parallel_to_fit > parallel_cap
            else ""
        )
        hint = (
            f"reduce attempt count to <= {max_attempts_to_fit}, or increase "
            f"max_parallel_attempts to >= {min_parallel_to_fit}{parallel_note}, or "
            "reduce the slowest attempt's timeout_ms + overhead + checker budget "
            f"to <= {max_slowest_total_ms} ms total"
        )
    raise ValueError(
        f"projected experiment budget {projected_ms} ms exceeds "
        f"MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS={MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS} ms. "
        f"Breakdown (slowest attempt): attempt_count={len(attempts)}, "
        f"max_parallel_attempts={max_parallel_attempts}, batches={batches}, "
        f"per_attempt_timeout_ms={slowest.timeout_ms}, "
        f"checker_timeout_ms={slowest.checker_timeout_ms}, "
        f"attempt_budget_ms={slowest.attempt_budget_ms}, "
        f"checker_budget_ms={slowest.checker_budget_ms}, "
        f"overhead_ms={slowest.attempt_budget_ms - slowest.timeout_ms}, "
        f"total_budget_ms={projected_ms}, "
        f"max_budget_ms={MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS} ({hint})"
    )


def _oversubscription_warning(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    names: Sequence[str],
    max_parallel_attempts: int,
) -> str | None:
    """Advisory-only: flag attempts whose config['num_workers'] combined with
    max_parallel_attempts may oversubscribe this machine's CPUs.

    This only observes the cooperative config["num_workers"] convention (see
    prompts.py's step 6 guidance) — a script that sets
    solver.parameters.num_workers any other way is invisible here. Never
    blocks the experiment; this is advisory, not a budget gate.
    """
    cpu_count = os.cpu_count() or 1
    offenders: list[tuple[str, int]] = []
    for name, attempt in zip(names, attempts, strict=True):
        num_workers = attempt.config.get("num_workers")
        if not isinstance(num_workers, int) or isinstance(num_workers, bool):
            continue
        if max_parallel_attempts * num_workers > cpu_count:
            offenders.append((name, num_workers))
    if not offenders:
        return None
    offenders_text = ", ".join(f"{name!r} (num_workers={n})" for name, n in offenders)
    max_workers = max(n for _, n in offenders)
    return (
        f"max_parallel_attempts={max_parallel_attempts} combined with attempt(s) "
        f"{offenders_text} may request up to {max_parallel_attempts * max_workers} "
        f"CP-SAT workers, exceeding this machine's cpu_count={cpu_count}; consider "
        "lowering num_workers or max_parallel_attempts."
    )


# Bounds the stderr tail folded into an errored attempt's ``message``, so a
# runaway traceback (or a script that dumps megabytes to stderr) can't blow up
# the attempt table.
_STDERR_SNIPPET_MAX_CHARS: int = 500

# Number of trailing non-blank stderr lines to keep. 2 covers the common case
# of a chained exception (a "During handling..." cause line followed by the
# final exception line) without the snippet growing past what a one-bullet
# attempt row should show.
_STDERR_SNIPPET_MAX_LINES: int = 2

# Bounds the raw stderr tail carried in structuredContent for a
# status="error" attempt — a much larger allowance than the one-line
# _STDERR_SNIPPET_MAX_CHARS used for the attempt table's `message`, so a
# client debugging a script exception can see the full traceback (not just
# its final line) without the printed table growing.
_ATTEMPT_STDERR_TAIL_MAX_CHARS: int = 4000


def _stderr_snippet(stderr: str) -> str | None:
    """Return the last couple of non-blank stderr lines, bounded, or ``None`` if empty.

    A Python traceback's most useful line — the exception type and message — is
    the last line printed, so tailing stderr surfaces it without parsing the
    traceback structure itself. Each line is truncated on its own (keeping its
    head, e.g. the exception type prefix) rather than tail-truncating the whole
    joined snippet, which could otherwise cut the prefix off a long final line.
    Lines are joined with " | " so the result stays single-line-safe for the
    plain-text, one-bullet-per-attempt formatter.
    """
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    tail = lines[-_STDERR_SNIPPET_MAX_LINES:]
    per_line_max = _STDERR_SNIPPET_MAX_CHARS // len(tail)
    truncated = [line if len(line) <= per_line_max else line[:per_line_max] for line in tail]
    return " | ".join(truncated)


def _run_attempt(
    index: int,
    attempt: CpsatPythonExperimentAttempt,
    name: str,
    *,
    default_timeout_ms: int,
    objective_sense: CpsatObjectiveSense | None,
    checker: str | None,
    problem: str | None,
    checker_timeout_ms: int | None,
    tracker: ChildProcessTracker | None,
) -> tuple[CpsatPythonExperimentAttemptResult, CpsatPythonResult | None]:
    """Run one attempt end to end; return its result row and, if accepted, its raw result.

    The config temp file (when ``attempt.config`` is non-empty) lives in a
    ``tempfile.TemporaryDirectory()`` scoped to exactly this call: it is created
    right before the child runs and removed right after ``run_cpsat_python``
    returns, whether the child exited cleanly, errored, or was tree-killed on
    timeout — no config temp file outlives its attempt.
    """
    timeout_ms = _effective_timeout_ms(attempt, default_timeout_ms)
    source_hash = text_sha256(attempt.source)
    config_hash = config_sha256(attempt.config)

    if attempt.config:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config_file(Path(tmp_dir), attempt.config)
            env = seed_config_env(seed=attempt.seed, config_path=config_path)
            result = run_cpsat_python(
                attempt.source, timeout_ms=timeout_ms, tracker=tracker, env=env
            )
    else:
        env = seed_config_env(seed=attempt.seed, config_path=None)
        result = run_cpsat_python(attempt.source, timeout_ms=timeout_ms, tracker=tracker, env=env)

    base_eligible, base_reject_reason = _attempt_eligibility(result, objective_sense)
    accepted = False
    checker_status = None
    message = base_reject_reason
    if not base_eligible and result.status == "error":
        snippet = _stderr_snippet(result.stderr)
        if snippet is not None:
            message = f"{base_reject_reason}: {snippet}"
    stderr_tail = (
        result.stderr[-_ATTEMPT_STDERR_TAIL_MAX_CHARS:]
        if result.status == "error" and result.stderr
        else None
    )
    if base_eligible and checker is not None:
        report = run_checker(
            checker=checker,
            run_result=result,
            problem=problem,
            timeout_ms=effective_checker_timeout_ms(
                checker_timeout_ms=checker_timeout_ms,
                default_timeout_ms=timeout_ms,
            ),
            tracker=tracker,
        )
        checker_status = report.status
        if report.status == "accepted":
            accepted = True
        else:
            message = f"checker {report.status}"
    elif base_eligible:
        accepted = True

    row = CpsatPythonExperimentAttemptResult(
        index=index,
        name=name,
        seed=attempt.seed,
        config_sha256=config_hash,
        source_sha256=source_hash,
        timeout_ms=timeout_ms,
        status=result.status,
        objective=result.objective,
        best_objective_bound=result.best_objective_bound,
        accepted=accepted,
        checker_status=checker_status,
        message=message,
        timed_out=result.timed_out,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        stderr_tail=stderr_tail,
    )
    return row, (result if accepted else None)


def _winner_sort_key(
    item: tuple[int, CpsatPythonResult], objective_sense: CpsatObjectiveSense | None
) -> tuple[float, int, int, int]:
    """Sort key for accepted candidates; the minimum is the winner (lower is better).

    In feasibility mode, status wins first. In optimization mode, base
    acceptance guarantees ``objective`` is a finite number, so negating it for
    ``maximize`` is safe. Ties break by stronger status, then faster
    ``duration_ms``, then earliest attempt order (the index component) — never
    completion order.
    """
    index, result = item
    if objective_sense is None:
        return 0.0, _STATUS_RANK[result.status], result.duration_ms, index
    objective = result.objective
    assert objective is not None  # guaranteed by optimization-mode acceptance
    objective_key = -objective if objective_sense == "maximize" else objective
    return objective_key, _STATUS_RANK[result.status], result.duration_ms, index


def run_cpsat_python_experiment(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    *,
    objective_sense: CpsatObjectiveSense | None = None,
    default_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    max_parallel_attempts: int = 1,
    problem: str | None = None,
    checker: str | None = None,
    checker_timeout_ms: int | None = None,
    include_winner_stdout: bool = True,
    tracker: ChildProcessTracker | None = None,
) -> CpsatPythonExperimentResult:
    """Run every explicit attempt and return the best accepted incumbent plus the table.

    Each attempt runs its own complete ``source`` (the server never diffs or
    merges attempts), optionally seeded via ``OPENCONSTRAINT_MCP_CPSAT_SEED`` and
    configured via ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` — both are cooperative
    protocols a script may ignore. Attempts run through a bounded thread pool
    (``max_parallel_attempts`` workers, default 1 = serial); results are
    assembled in original attempt order regardless of completion order.

    Acceptance is two ordered gates (short-circuiting like the save path): base
    acceptance (status ∈ {optimal, feasible, timeout}, non-empty solution, and
    in optimization mode only a finite numeric objective), then — only for
    base-eligible attempts — the optional checker gate (accepted iff the checker
    returns ``accepted``). The checker is never spent on an attempt that already
    failed base acceptance.

    In optimization mode (``objective_sense`` set), the winner is the accepted
    attempt with the best objective for ``objective_sense``, breaking ties by
    stronger status then earliest attempt order. In feasibility mode
    (``objective_sense`` omitted/``None``), the winner is the accepted attempt
    with the strongest status, then earliest attempt order. Returns a
    ``CpsatPythonExperimentResult`` with ``status="winner"`` and the winning
    ``CpsatPythonResult``/index/name, or ``status="no_winner"`` when nothing was
    accepted. A ``timeout`` winner is a reportable best incumbent, not a savable
    one (it fails the save reported gate). Whenever there is a winner,
    ``warnings`` always carries a reproducibility disclaimer — an experiment
    result is one observed run, not a guarantee that
    ``save_verified_cpsat_python`` will reproduce the same objective on
    replay — alongside any ``num_workers``-oversubscription advisory.

    Raises ``ValueError`` for an invalid request — including a projected budget
    over ``MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS`` or a ``max_parallel_attempts``
    over the server cap — before any child is spawned.
    """
    validated_objective_sense = _validate_objective_sense(objective_sense)
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if default_timeout_ms <= 0:
        raise ValueError("default_timeout_ms must be positive")
    validated_max_parallel = _validate_max_parallel_attempts(max_parallel_attempts)
    names = _validate_attempts(attempts)
    oversubscription_warning = _oversubscription_warning(attempts, names, validated_max_parallel)

    _check_wall_clock_budget(
        attempts,
        default_timeout_ms=default_timeout_ms,
        max_parallel_attempts=validated_max_parallel,
        checker_present=checker is not None,
        checker_timeout_ms=checker_timeout_ms,
    )

    start = time.monotonic()

    def _run(
        item: tuple[int, CpsatPythonExperimentAttempt],
    ) -> tuple[CpsatPythonExperimentAttemptResult, CpsatPythonResult | None]:
        index, attempt = item
        return _run_attempt(
            index,
            attempt,
            names[index],
            default_timeout_ms=default_timeout_ms,
            objective_sense=validated_objective_sense,
            checker=checker,
            problem=problem,
            checker_timeout_ms=checker_timeout_ms,
            tracker=tracker,
        )

    with ThreadPoolExecutor(max_workers=validated_max_parallel) as pool:
        # map() yields in input order regardless of completion order, so the
        # pool's own concurrency is never traded away for ordered results.
        results = list(pool.map(_run, enumerate(attempts)))

    attempt_rows = [row for row, _ in results]
    accepted = [(index, result) for index, (_, result) in enumerate(results) if result is not None]
    elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
    winner = (
        min(accepted, key=lambda item: _winner_sort_key(item, validated_objective_sense))
        if accepted
        else None
    )

    source_sha256 = [row.source_sha256 for row in attempt_rows]
    checker_sha = text_sha256(checker) if checker is not None else None
    problem_sha = text_sha256(problem) if problem is not None else None

    winner_index = None
    winner_name = None
    winner_result = None
    if winner is not None:
        winner_index, winner_result = winner
        winner_name = names[winner_index]

    if not include_winner_stdout and winner_result is not None:
        winner_result = winner_result.model_copy(update={"stdout": _WINNER_STDOUT_OMITTED_SENTINEL})

    warnings = [oversubscription_warning] if oversubscription_warning else []
    if winner is not None:
        warnings.append(_REPRODUCIBILITY_WARNING)

    return CpsatPythonExperimentResult(
        status="winner" if winner is not None else "no_winner",
        winner_index=winner_index,
        winner_name=winner_name,
        winner=winner_result,
        attempts=attempt_rows,
        elapsed_ms=elapsed_ms,
        objective_sense=validated_objective_sense,
        selection_policy=_selection_policy(validated_objective_sense),
        source_sha256=source_sha256,
        checker_sha256=checker_sha,
        problem_sha256=problem_sha,
        warnings=warnings,
    )
