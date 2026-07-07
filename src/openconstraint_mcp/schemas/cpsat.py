from __future__ import annotations

import math
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StrictInt,
    computed_field,
    field_validator,
    model_validator,
)

from .artifacts import SavedModelArtifact
from .job_state import RESULT_BEARING_STATES, JobState

# ---------------------------------------------------------------------------
# CP-SAT Python executor output models (moved from pyexec/core.py per D7 so
# CpsatPythonJobStatus can reference CpsatPythonResult without a
# schemas → pyexec.core edge that would break the dependency-free leaf).
# ---------------------------------------------------------------------------

CpsatStatus = Literal["optimal", "feasible", "infeasible", "unknown", "error", "timeout"]


class CpsatPythonResult(BaseModel):
    status: CpsatStatus
    solution: dict | None
    objective: float | int | None
    # OR-Tools' solver.best_objective_bound — a diagnostic bound, not a proven
    # objective. Useful even when status="unknown" and no incumbent was found,
    # since a script may still emit it. None for a script that never reports it
    # (backward compatible with scripts predating this field), reports a
    # non-finite/non-numeric value (normalized like `objective`), or is a pure
    # feasibility problem (no objective — OR-Tools returns a meaningless 0.0
    # rather than raising, so a conforming script reports None instead).
    best_objective_bound: float | int | None = None
    stdout: str
    stderr: str
    return_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int


def cpsat_job_state_for_result(result: CpsatPythonResult) -> JobState:
    """Map a produced ``CpsatPythonResult`` to its terminal ``JobState`` (D3).

    ``timeout`` → ``timeout`` (result-bearing; partial recovered).
    All other statuses — including ``error`` — → ``succeeded``: ``status="error"``
    is a normal structured verdict (the child ran and produced output), not a
    job-machinery failure. The job "succeeded at running the code"; the embedded
    ``CpsatPythonResult.status`` tells the client whether the code itself errored.
    ``failed`` is reserved for a worker exception with no result; ``cancelled``
    for user cancel — both result-absent, consistent with ``result present ⇔
    state ∈ {succeeded, timeout}``.
    """
    if result.timed_out or result.status == "timeout":
        return "timeout"
    return "succeeded"


class CpsatCheckerReport(BaseModel):
    """Result of running an optional checker script against the CP-SAT solution.

    ``status`` is the normalized server verdict: ``accepted`` only when the
    checker returned ``accepted`` with an empty ``errors`` list; ``rejected``
    when the checker rejected; ``timeout`` on a wall-clock timeout;
    ``error`` for malformed output, nonzero exit, truncation, or
    ``accepted``+non-empty-errors (self-contradictory output).
    ``stdout``, ``stderr``, and ``details`` are raw checker output and are
    NOT persisted in the manifest (only the scalar summary is saved).

    Defined before ``CpsatPythonJobStatus`` (which embeds it) so the job-status
    model builds without a deferred forward reference.
    """

    status: Literal["accepted", "rejected", "error", "timeout"]
    errors: list[str]
    details: dict | None = None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


class CpsatPythonJobStatus(BaseModel):
    """A background CP-SAT Python job's status snapshot.

    Mirrors ``SolveJobStatus``: ``result`` is present IFF ``state`` is a
    result-bearing terminal state (``succeeded`` or ``timeout``), absent for
    ``queued``/``running`` and for ``failed``/``cancelled``. The invariant
    ``result present ⇔ state ∈ {succeeded, timeout}`` is enforced.
    ``timeout_ms`` echoes the caller's SOLVER-child cap only, so a polling
    client can pace the solve phase (``remaining ≈ timeout_ms - elapsed_ms``).
    A checked job may remain ``running`` beyond ``timeout_ms`` for the checker
    phase, up to the echoed ``checker_timeout_ms``. ``message`` carries
    failure/cancel detail; a ``failed`` job has no result so its diagnostic
    lives only in ``message``.

    Checker fields (diagnostic only — never a save gate; saving still replays
    through ``save_verified_cpsat_python``):
    - ``checker`` is the checker's report on a result-bearing job whose
      supplied checker ran; ``CpsatCheckerReport.status`` is the verdict (no
      duplicate ``checker_status`` field exists).
    - ``checker_skipped_reason`` is set only when a supplied checker did not
      run (result not checker-eligible). Mutually exclusive with ``checker``,
      and both are restricted to result-bearing states.
    - ``checker_timeout_ms`` is a request echo like ``timeout_ms`` (constant
      across states): the effective checker timeout when a checker was
      supplied (the explicit value, else the ``timeout_ms`` default), ``None``
      when no checker was supplied.
    """

    job_id: str
    state: JobState
    timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: CpsatPythonResult | None = None
    message: str | None = None
    checker: CpsatCheckerReport | None = None
    checker_skipped_reason: str | None = None
    checker_timeout_ms: int | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> CpsatPythonJobStatus:
        has_result = self.result is not None
        expects_result = self.state in RESULT_BEARING_STATES
        if has_result and not expects_result:
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded' or 'timeout')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} requires a result "
                "(state 'succeeded'/'timeout' ⇔ result is present)"
            )
        if self.checker is not None and self.checker_skipped_reason is not None:
            raise ValueError(
                "CpsatPythonJobStatus checker and checker_skipped_reason are mutually "
                "exclusive (a checker either ran or was skipped, never both)"
            )
        if (self.checker is not None or self.checker_skipped_reason is not None) and (
            not expects_result
        ):
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} must not carry checker or "
                "checker_skipped_reason (checker outcomes appear only on state "
                "'succeeded' or 'timeout')"
            )
        return self


# ---------------------------------------------------------------------------
# CP-SAT Python verification gate schemas
# ---------------------------------------------------------------------------

CpsatObjectiveSense = Literal["maximize", "minimize"]

# The highest gate that passed during a save attempt. "none" means even the
# reported gate failed (nothing was saved). The level never claims a save
# happened — combine with `saved` for that.
CpsatVerificationLevel = Literal["none", "reported", "expectation", "checked"]


class CpsatExpectation(BaseModel):
    """Caller-supplied objective threshold for a CP-SAT save gate.

    A threshold gate is NOT an optimality proof — it only checks that the
    script's reported objective meets the supplied bound. For satisfaction
    problems (no objective), omit this and use a checker instead.
    """

    objective_sense: CpsatObjectiveSense
    objective_threshold: float

    @field_validator("objective_threshold", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("objective_threshold must be a number, not a bool")
        return value

    @field_validator("objective_threshold", mode="after")
    @classmethod
    def _reject_non_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("objective_threshold must be a finite number (not NaN or ±inf)")
        return value


class SaveVerifiedPythonResult(BaseModel):
    """Outcome of a save_verified_cpsat_python request.

    ``saved`` is computed from ``reason``: True iff ``reason`` is None and
    ``target_dir`` is set. ``verification_level`` is the highest gate that
    passed — combine with ``saved`` to distinguish a successful save from a
    failed gate at the same level.

    Gate short-circuit order: reported → expectation → checker. Every gate
    downstream of the first failure carries its None/False default.
    """

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
    files: list[SavedModelArtifact] = Field(default_factory=list)

    # Verification gate summary — always present
    verification_level: CpsatVerificationLevel = "none"
    reported_passed: bool = False
    expectation: CpsatExpectation | None = None
    expectation_passed: bool | None = None
    checker: CpsatCheckerReport | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def saved(self) -> bool:
        return self.reason is None and self.target_dir is not None


# ---------------------------------------------------------------------------
# CP-SAT Python explicit-experiment schemas
# ---------------------------------------------------------------------------

# An experiment's overall outcome. ``winner`` ⇔ an accepted attempt was selected
# (its full ``CpsatPythonResult`` plus ``winner_index``/``winner_name`` are set);
# ``no_winner`` ⇔ no attempt was accepted. This ⇔ is enforced below.
CpsatExperimentStatus = Literal["winner", "no_winner"]

# How an experiment winner is chosen, surfaced as a typed (not free-form) value:
# optimization: best objective for the requested sense, then stronger status
# (optimal > feasible > timeout), then faster duration_ms, then earliest attempt
# order. Feasibility: stronger status, then faster duration_ms, then earliest
# attempt order. Never completion order.
CpsatExperimentSelectionPolicy = Literal[
    "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
    "accepted_status_then_duration_then_attempt_order",
]

# A per-attempt checker verdict (None when no checker was supplied for the
# experiment, or when the attempt failed base acceptance and the checker was
# never run on it).
CpsatExperimentCheckerStatus = Literal["accepted", "rejected", "error", "timeout"]


class CpsatPythonExperimentAttempt(BaseModel):
    """One explicit attempt in a ``run_cpsat_python_experiment`` request.

    ``source`` is a complete, independent CP-SAT Python script — the server does
    not diff or merge attempts, so each one must be runnable on its own. ``name``
    is optional; an unnamed attempt is assigned the display label
    ``attempt-{index}`` at execution time (see ``CpsatPythonExperimentAttemptResult``).
    ``seed`` injects ``OPENCONSTRAINT_MCP_CPSAT_SEED`` for a cooperating script,
    identically to the save path's seeded replay. ``config`` is an OPAQUE JSON
    object the server writes to a temp file and points
    ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` at — the server never sets OR-Tools
    parameters itself; only a cooperating script that reads the env var and
    applies fields it understands benefits from it. An empty ``config`` (``{}``)
    is normalized to "no config" everywhere (no temp file, no env var, no hash).
    ``timeout_ms`` overrides the request's ``default_timeout_ms`` for this one
    attempt.
    """

    name: str | None = None
    source: str
    seed: StrictInt | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_ms: int | None = None


class CpsatPythonExperimentAttemptResult(BaseModel):
    """One attempt's observed outcome in an experiment's attempt table.

    ``name`` is always the RESOLVED display label (explicit, or the
    ``attempt-{index}`` default) — never ``None`` — so it can double as
    ``winner_name`` and as the uniqueness key attempts were validated over.
    ``config_sha256`` is the canonical-JSON hash of this attempt's ``config``,
    or ``None`` when the attempt ran with no config (``{}`` and omitted are
    equivalent). ``source_sha256`` is this attempt's exact source text hash —
    provenance for a later save's replay-consistency check. ``checker_status``
    is ``None`` when no checker was supplied for the experiment, or this
    attempt failed base acceptance before the checker could run.
    ``stderr_tail`` is a bounded tail of ``stderr``, populated only when
    ``status == "error"`` and ``stderr`` is non-empty — ``None`` for every
    other status (including ``timeout``, ``infeasible``, ``feasible``,
    ``optimal``, or an ``error`` with empty ``stderr``). This is separate
    from ``message``'s short single-line snippet: ``message`` stays concise
    for the printed attempt table, ``stderr_tail`` is a larger bounded tail
    for debugging, carried only in ``structuredContent``.
    ``best_objective_bound`` is diagnostic only — it is never consulted for
    ``accepted``/winner selection, so an ``unknown`` attempt with no
    incumbent can still surface search progress via this field.
    """

    index: int
    name: str
    seed: int | None = None
    config_sha256: str | None = None
    source_sha256: str
    timeout_ms: int
    status: CpsatStatus
    objective: float | int | None
    best_objective_bound: float | int | None = None
    accepted: bool
    checker_status: CpsatExperimentCheckerStatus | None = None
    message: str | None = None
    timed_out: bool
    truncated: bool
    duration_ms: int
    stderr_tail: str | None = None


class CpsatPythonExperimentResult(BaseModel):
    """Outcome of a CP-SAT Python explicit experiment: the winner and the full table.

    ``status="winner"`` carries the winning attempt's ``winner_index`` (a 0-based
    handle into ``attempts``), ``winner_name`` (equal to
    ``attempts[winner_index].name``), and the full ``winner`` ``CpsatPythonResult``;
    ``status="no_winner"`` leaves all three ``None`` because no attempt was
    accepted. The invariant ``winner ⇔ winner_index ⇔ winner_name ⇔ status ==
    "winner"`` is enforced, and — going one step further than
    ``PortfolioSolveResult`` — ``winner_index`` is bounds-checked against
    ``attempts`` and ``winner_name`` is checked to match the winning row's own
    ``name`` HERE, in the schema, so the save gate and ``server.py`` formatting
    can trust the winner fields without re-checking them defensively (unlike
    ``PortfolioSolveResult``'s ``winner_index``, whose bounds are instead
    checked eagerly by the minizinc save path's
    ``_validate_portfolio_result_consistency``).

    A ``timeout`` winner is a REPORTABLE best incumbent, not a SAVABLE one: it
    fails ``save_verified_cpsat_python``'s reported gate (``optimal``/``feasible``
    only).

    ``source_sha256`` is one hex digest per attempt, index-aligned with
    ``attempts`` (so ``source_sha256[i] == attempts[i].source_sha256``).
    ``checker_sha256``/``problem_sha256`` are the hashes of the experiment's
    shared ``checker``/``problem`` text, or ``None`` when omitted. All three are
    provenance, computed once for the request that produced this result — not a
    save-time trust decision (the save gate always re-runs the winner fresh).

    ``warnings`` carries non-blocking advisory messages and defaults to an
    empty list. Two independent sources populate it: the
    ``num_workers``-oversubscription check (only when triggered), and — added
    unconditionally whenever ``status == "winner"`` — a reproducibility
    disclaimer noting that an experiment winner is one observed run, not a
    guarantee that ``save_verified_cpsat_python``'s fresh re-run will find the
    same objective.
    """

    status: CpsatExperimentStatus
    winner_index: int | None = None
    winner_name: str | None = None
    winner: CpsatPythonResult | None = None
    attempts: list[CpsatPythonExperimentAttemptResult] = Field(default_factory=list)
    elapsed_ms: int
    objective_sense: CpsatObjectiveSense | None
    selection_policy: CpsatExperimentSelectionPolicy
    source_sha256: list[str] = Field(default_factory=list)
    checker_sha256: str | None = None
    problem_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _winner_presence_matches_status(self) -> CpsatPythonExperimentResult:
        has_winner = self.winner is not None
        index_present = self.winner_index is not None
        name_present = self.winner_name is not None
        status_winner = self.status == "winner"
        if not (has_winner == index_present == name_present == status_winner):
            raise ValueError(
                "CpsatPythonExperimentResult requires winner, winner_index, "
                "winner_name, and status=='winner' to agree (all present "
                "together or all absent)"
            )
        self._validate_winner_attempt_fields()
        return self

    def _validate_winner_attempt_fields(self) -> None:
        if self.winner_index is None:
            return
        if not (0 <= self.winner_index < len(self.attempts)):
            raise ValueError(
                f"winner_index {self.winner_index} is out of range for "
                f"{len(self.attempts)} attempts"
            )
        if self.attempts[self.winner_index].name != self.winner_name:
            raise ValueError(
                "winner_name must equal attempts[winner_index].name "
                f"({self.winner_name!r} != {self.attempts[self.winner_index].name!r})"
            )
