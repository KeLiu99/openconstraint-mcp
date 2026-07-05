from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StrictInt,
    computed_field,
    field_validator,
    model_validator,
)

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


# A background CP-SAT job's lifecycle state. Mirrors JobState; result-bearing
# set is {succeeded, timeout} — same invariant as SolveJobStatus (D3).
CpsatJobState = Literal["queued", "running", "succeeded", "failed", "timeout", "cancelled"]
_CPSAT_RESULT_BEARING_STATES: frozenset[CpsatJobState] = cast(
    "frozenset[CpsatJobState]", frozenset({"succeeded", "timeout"})
)


def cpsat_job_state_for_result(result: CpsatPythonResult) -> CpsatJobState:
    """Map a produced ``CpsatPythonResult`` to its terminal ``CpsatJobState`` (D3).

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


class CpsatPythonJobStatus(BaseModel):
    """A background CP-SAT Python job's status snapshot.

    Mirrors ``SolveJobStatus``: ``result`` is present IFF ``state`` is a
    result-bearing terminal state (``succeeded`` or ``timeout``), absent for
    ``queued``/``running`` and for ``failed``/``cancelled``. The invariant
    ``result present ⇔ state ∈ {succeeded, timeout}`` is enforced.
    ``timeout_ms`` echoes the caller's cap so a polling client can pace itself
    (``remaining ≈ timeout_ms - elapsed_ms``). ``message`` carries failure/cancel
    detail; a ``failed`` job has no result so its diagnostic lives only in ``message``.
    """

    job_id: str
    state: CpsatJobState
    timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: CpsatPythonResult | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> CpsatPythonJobStatus:
        has_result = self.result is not None
        expects_result = self.state in _CPSAT_RESULT_BEARING_STATES
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
        return self


class RuntimeStatus(BaseModel):
    installed: bool
    runtime_dir: str
    minizinc_binary: str | None = None


class SolverCapabilities(BaseModel):
    # Read from the solver config's stdFlags (--solvers-json): which standard
    # solve controls the managed runtime declares for this solver. ENFORCED — a
    # requested all_solutions/free_search/parallel/random_seed is rejected before
    # solving when the matching flag is absent (matched by canonical solver id).
    supports_all_solutions: bool = False  # -a
    supports_free_search: bool = False  # -f
    supports_parallel: bool = False  # -p
    supports_random_seed: bool = False  # -r
    # NOT stdFlags-derived. The conservative, canonical num_solutions gate
    # (org.gecode.gecode / org.chuffed.chuffed only). org.gecode.gist lists -n in
    # stdFlags but is excluded, so this deliberately diverges from std_flags.
    supports_num_solutions: bool = False  # -n
    # Raw stdFlags, advisory/informational. May include flags the server exposes
    # no named control for; NOT a passthrough surface.
    std_flags: list[str] = Field(default_factory=list)


class SolverInfo(BaseModel):
    id: str
    name: str
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    capabilities: SolverCapabilities = Field(default_factory=SolverCapabilities)


class SolverList(BaseModel):
    solvers: list[SolverInfo]
    capability_note: str = "Detailed solver capabilities are available on request for every solver."


SolveStatus = Literal[
    "satisfied",
    "optimal",
    "unsatisfiable",
    "unknown",
    "unbounded",
    "unsat_or_unbounded",
    "error",
    "timeout",
]


# A `--solution-checker` aggregate verdict — honest, NOT pass/fail (Decision D4).
# `completed` means the checker ran for every produced solution with no nested
# UNSATISFIABLE; it does NOT mean "all author-correct" (author CORRECT/INCORRECT
# text is a convention the server never adjudicates). `violation` is the one
# verdict the server asserts on its own (a constraint-style checker rejected a
# solution). `error` covers a stream ERROR, a nonzero return code (broken/missing
# checker, wrong suffix), or a checker-verdict count that misaligns with the
# produced solutions.
CheckerStatus = Literal["completed", "violation", "no_solution", "error", "timeout"]


class SolutionCheck(BaseModel):
    """One produced solution's checker verdict, index-aligned with the solve's ``solutions``.

    ``violation`` is the one machine-readable signal — True iff this solution
    carried a nested checker ``status: UNSATISFIABLE`` (a constraint-style
    rejection). ``output`` is the best-effort per-solution verdict text (the
    author's ``CORRECT``/``INCORRECT`` text, surfaced verbatim and NOT
    interpreted) or, for a rejection, the violation diagnostic; the verbatim
    record lives in ``CheckerReport.transcript``.
    """

    violation: bool
    output: str


class CheckerReport(BaseModel):
    """A ``--solution-checker`` run's per-solution verdicts and honest aggregate.

    Populated on a ``SolveResult`` only when a checker was supplied (otherwise
    ``SolveResult.checker`` is ``None``). ``checks`` is the best-effort structured
    view, one entry per produced solution, index-aligned with the solve's
    ``solutions`` when ``status`` is ``completed`` or ``violation`` — and that
    ``solutions`` list INCLUDES checker-rejected solutions (a violation does not
    suppress the solution), so on ``status == "violation"`` consult the
    per-solution ``checks`` rather than assuming every produced solution is valid.
    ``transcript`` is the raw ``--json-stream`` transcript (solve + checker
    objects) exactly as the subprocess emitted it — the AUTHORITATIVE checker
    record; ``SolveResult.stdout`` carries only the reconstructed solution text
    (the solve parser drops checker objects), which is why the transcript is
    preserved here. The checker never proves optimality — ``SolveResult.status``
    remains the only proof of completeness/optimality.
    """

    status: CheckerStatus
    checks: list[SolutionCheck] = Field(default_factory=list)
    transcript: str


class SolveResult(BaseModel):
    status: SolveStatus
    solver: str
    return_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    elapsed_ms: int
    statistics: dict[str, str] = Field(default_factory=dict)
    # Structured solutions parsed from the driver's json-stream (the `output.json`
    # section of each solution object, with `_objective` removed). `solution` is
    # the last/best solution, `solutions` the full ordered list (improving
    # sequence for optimization, enumeration for `all_solutions`), and `objective`
    # the best solution's `_objective` — None for satisfaction and no-solution runs.
    solution: dict[str, Any] | None = None
    solutions: list[dict[str, Any]] = Field(default_factory=list)
    objective: int | float | None = None
    # Populated iff a solution checker was supplied to the solve (the optional
    # `--solution-checker`); None for an ordinary solve. The checker validates
    # each produced solution; it never proves optimality (see CheckerReport).
    checker: CheckerReport | None = None


# A background solve job's lifecycle state. `queued`/`running` are non-terminal;
# `succeeded`/`failed`/`timeout`/`cancelled` are terminal. See `job_state_for_result`
# and `SolveJobStatus` for the result-presence invariant (D1.9).
JobState = Literal["queued", "running", "succeeded", "failed", "timeout", "cancelled"]
# The terminal states that carry a produced `SolveResult`. The load-bearing D1.9
# invariant: a `SolveJobStatus` has a `result` IFF its state is one of these
# (`result present ⇔ state ∈ {succeeded, timeout}`). For `failed` this is one-way
# only — `failed ⇒ result is None`, but `result is None` also holds for
# `queued`/`running`/`cancelled`.
_RESULT_BEARING_STATES: frozenset[JobState] = cast(
    "frozenset[JobState]", frozenset({"succeeded", "timeout"})
)


def job_state_for_result(result: SolveResult) -> JobState:
    """Map a produced ``SolveResult`` to its terminal ``JobState`` (D1.9).

    Total over all eight ``SolveStatus`` values for the *result-present* paths: a
    subprocess timeout (``result.timed_out``) or a stream ``timeout`` verdict maps
    to ``"timeout"``; every other status — INCLUDING ``"error"`` — maps to
    ``"succeeded"``, because ``status="error"`` is a normal structured
    solver/driver verdict (e.g. cp-sat rejecting an out-of-range ``random_seed``),
    not a job-machinery failure. The result-absent terminal states are set by the
    registry, not derived here: ``"failed"`` for a runner exception (no
    ``SolveResult`` produced) and ``"cancelled"`` for user cancellation — both
    result-absent, consistent with ``result present ⇔ state ∈ {succeeded, timeout}``.
    """
    if result.timed_out or result.status == "timeout":
        return "timeout"
    return "succeeded"


class SolveJobStatus(BaseModel):
    """A background solve job's status snapshot.

    ``result`` is present IFF ``state`` is a result-bearing terminal state
    (``succeeded`` or ``timeout``); it is ``None`` for ``queued``/``running`` (not
    finished) and for ``failed``/``cancelled`` (no usable result). This is the
    load-bearing D1.9 invariant — ``result present ⇔ state ∈ {succeeded, timeout}``
    — and it is *enforced*, so a client can branch on ``state`` and trust
    ``result``'s presence (``result is None`` alone does not imply ``failed``).
    Partial mid-run statistics are not provided in this increment: a
    ``running`` job reports ``state``, ``elapsed_ms``, and the requested
    ``timeout_ms`` only; ``statistics`` and the ``SolveResult`` populate on
    terminal success. ``timeout_ms`` echoes the caller's solve time-limit (the
    same value passed to ``submit_solve_job``) and is present in every state, so
    a polling client can pace itself against the remaining budget
    (``remaining ≈ timeout_ms - elapsed_ms``) instead of guessing a poll
    interval. ``message`` carries failure/cancel detail and never replaces a
    ``SolveResult.stderr`` (a ``failed`` job has no result, so its diagnostic
    lives only in ``message``).
    """

    job_id: str
    state: JobState
    solver: str
    timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: SolveResult | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> SolveJobStatus:
        has_result = self.result is not None
        expects_result = self.state in _RESULT_BEARING_STATES
        if has_result and not expects_result:
            raise ValueError(
                f"SolveJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded' or 'timeout')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"SolveJobStatus state={self.state!r} requires a result "
                "(state 'succeeded'/'timeout' ⇔ result is present)"
            )
        return self


# One portfolio attempt's lifecycle as the portfolio observed it. `submitted`/
# `running` are non-terminal (the poll budget can return while an attempt is still
# running); `succeeded`/`timeout`/`failed`/`cancelled` mirror the registry's
# terminal `JobState`; `rejected` marks an attempt that was never admitted (the
# atomic `submit_many` makes the happy path admit all or none, so `rejected` is a
# defensive vocabulary entry, not produced on a returned result).
PortfolioAttemptState = Literal[
    "submitted",
    "running",
    "succeeded",
    "timeout",
    "failed",
    "cancelled",
    "rejected",
]
# `submitted`/`running` are the only non-terminal attempt states; everything
# else — including `rejected`, which marks an attempt that will never run — is
# final and never changes on a later poll.
PORTFOLIO_ATTEMPT_TERMINAL_STATES: frozenset[PortfolioAttemptState] = cast(
    "frozenset[PortfolioAttemptState]",
    frozenset({"succeeded", "timeout", "failed", "cancelled", "rejected"}),
)
# A portfolio's overall outcome. `winner` ⇔ an attempt was selected (its winning
# `SolveResult` is attached and `winner_index` is set); the winning result's own
# `status` says whether the win was decisive (a proof/solution) or a best-available
# fallback (timeout/unknown/error). `no_winner` ⇔ no attempt produced a usable
# result (all failed/cancelled). This ⇔ is enforced on `PortfolioSolveResult`.
PortfolioStatus = Literal["winner", "no_winner"]


class PortfolioAttempt(BaseModel):
    """One model/solver/seed attempt in a portfolio race and its final observed state.

    Carries enough to explain the attempt without re-polling the registry after
    the portfolio returns: which formulation it ran (`model_index`, a 0-based handle
    into the caller's `models` list), its `solver`/`seed` (the exact requested or
    generated seed value, or ``None`` when the portfolio ran unseeded), the
    portfolio-level `state`,
    the raw registry `job_state` (``None`` if it was never admitted), and — when a
    ``SolveResult`` was produced — the `result_status` and `objective`. `message`
    carries failure/cancel detail; `job_id` is the registry handle (``None`` when
    not admitted). The winning formulation is `models[attempts[winner_index].model_index]`.
    `checker_status` is the attempt's own checker verdict (``None`` when no checker
    was supplied to the race, or the attempt never produced a result) — purely
    observational: it does not affect winner selection, so a checker-violated
    attempt can still win the race.
    """

    index: int
    model_index: int
    solver: str
    seed: int | None = None
    timeout_ms: int
    state: PortfolioAttemptState
    job_id: str | None = None
    job_state: JobState | None = None
    result_status: SolveStatus | None = None
    objective: int | float | None = None
    elapsed_ms: int | None = None
    message: str | None = None
    checker_status: CheckerStatus | None = None


class PortfolioSolveControls(BaseModel):
    """The shared solve controls every attempt in a portfolio race ran with.

    Provenance, recorded at admission time like the sha256 hashes on
    ``PortfolioSolveResult``: these four controls are applied uniformly to every
    attempt's ``SolveRequest``, so they live once on the race result rather than
    on each ``PortfolioAttempt`` row. A save that attaches the race as
    ``portfolio_result`` must replay with the same values — unlike
    ``timeout_ms``, which is a budget rather than search configuration (and is
    already recorded per attempt), these change what the solver searches, so a
    mismatch means the save is not replaying the winning attempt's run.
    """

    free_search: bool
    parallel: int | None
    all_solutions: bool
    num_solutions: int | None


class PortfolioSolveResult(BaseModel):
    """Outcome of a solver-portfolio race over the managed runtime.

    ``status="winner"`` carries the winning attempt's `winner_index` and full
    `winner` ``SolveResult``; ``status="no_winner"`` leaves both ``None`` because
    no attempt produced a usable result. The invariant ``winner present ⇔
    winner_index present ⇔ status == "winner"`` is enforced, so a client can branch
    on `status` and trust the winner fields. `attempts` records every attempt's
    final state (the winner plus the cancelled/terminal losers) so the loser fates
    are visible without polling child jobs. `selection_policy` documents how the
    winner was chosen (e.g. ``"first-decisive-result"``).

    ``models_sha256``/``data_sha256``/``checker_sha256`` are provenance: sha256 hex
    digests of the exact ``models``/``data``/``checker`` text the race was admitted
    with, computed once at admission time (before any attempt ran) rather than at
    save time, since a later save-time input is untrusted client round-trip data —
    the binding must reflect what actually ran. ``models_sha256`` is one digest per
    formulation, index-aligned with the caller's ``models`` list (so
    ``models_sha256[attempt.model_index]`` names the exact text an attempt ran);
    ``data_sha256``/``checker_sha256`` are ``None`` iff the race ran with no
    ``data``/``checker`` supplied (an empty-string input, if ever accepted, still
    hashes to ``sha256("")`` — never ``None``). They let a later save-with-result
    flow verify the race ran against the exact same text it is being asked to save,
    and let a persisted experiment log stay self-describing after it leaves the
    original request. This task only computes and records these fields — no gating
    on them happens here: a checker-hash mismatch between race and save is not a
    rejection, the fresh save-time checker decides, and the recorded hash only lets
    a log say which checker gated the race.

    ``solve_controls`` records the shared search configuration the race ran under
    (see ``PortfolioSolveControls``) — captured at admission time for the same
    round-trip-trust reason as the hashes above.
    """

    status: PortfolioStatus
    winner_index: int | None = None
    winner: SolveResult | None = None
    attempts: list[PortfolioAttempt] = Field(default_factory=list)
    elapsed_ms: int
    selection_policy: str
    models_sha256: list[str]
    data_sha256: str | None
    checker_sha256: str | None
    solve_controls: PortfolioSolveControls

    @model_validator(mode="after")
    def _winner_presence_matches_status(self) -> PortfolioSolveResult:
        has_winner = self.winner is not None
        index_present = self.winner_index is not None
        status_winner = self.status == "winner"
        if not (has_winner == index_present == status_winner):
            raise ValueError(
                "PortfolioSolveResult requires winner, winner_index, and "
                "status=='winner' to agree (all present together or all absent)"
            )
        return self

    @model_validator(mode="after")
    def _attempt_model_indices_in_range(self) -> PortfolioSolveResult:
        for attempt in self.attempts:
            if not (0 <= attempt.model_index < len(self.models_sha256)):
                raise ValueError(
                    f"attempt index={attempt.index} has model_index="
                    f"{attempt.model_index}, out of range for {len(self.models_sha256)} "
                    "models_sha256 entries"
                )
        return self


# A background portfolio job's lifecycle. Unlike a single solve job there is no
# `queued`/`timeout`/`failed`: the race's attempts are admitted to the solve
# registry the moment the portfolio is submitted (so a full-queue rejection surfaces
# synchronously, not as a job), winner-selection is a pure function of the attempts'
# statuses (it cannot itself fail — a per-attempt failure is captured in the
# attempts table, and a race with no decisive winner is still a SUCCESSFUL
# orchestration carrying a `no_winner` PortfolioSolveResult), and only `succeeded`
# is result-bearing.
PortfolioJobState = Literal["running", "succeeded", "cancelled"]


class PortfolioJobStatus(BaseModel):
    """A background portfolio race's status snapshot (the async face of a portfolio).

    The portfolio analogue of ``SolveJobStatus``: ``submit_portfolio_job`` returns
    one with ``state="running"`` immediately so the race never blocks past a
    synchronous MCP client timeout, and ``get_portfolio_job`` polls it to a terminal
    state. ``result`` (the full ``PortfolioSolveResult``, winner and the
    cancelled-loser table alike) is present IFF ``state == "succeeded"`` — this is
    enforced, so a client branches on ``state`` and trusts ``result``'s presence.
    A ``no_winner`` race is ``succeeded`` (the orchestration completed); ``cancelled``
    means the client stopped the race. Mid-race statistics are not provided: a
    ``running`` job reports only ``state``, ``elapsed_ms``, and the requested
    ``per_attempt_timeout_ms`` so a client can pace polling against the per-attempt
    budget instead of guessing an interval.
    """

    job_id: str
    state: PortfolioJobState
    per_attempt_timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: PortfolioSolveResult | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> PortfolioJobStatus:
        has_result = self.result is not None
        expects_result = self.state == "succeeded"
        if has_result and not expects_result:
            raise ValueError(
                f"PortfolioJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"PortfolioJobStatus state={self.state!r} requires a result "
                "(state 'succeeded' ⇔ result is present)"
            )
        return self


CheckStatus = Literal[
    "ok",  # rc == 0 — the model compiled (flattened) for the chosen solver
    "error",  # rc != 0 — syntax/type/include/domain/unsupported-construct error (see stderr)
    "timeout",  # subprocess wall-clock cap fired during compilation
]


class CheckResult(BaseModel):
    status: CheckStatus
    solver: str
    stdout: str
    stderr: str
    elapsed_ms: int


SaveStatus = Literal[
    "saved",  # verification gate passed and the artifact directory was written
    "not_verified",  # a check/solve/checker gate failed — NOTHING was written
]

# The fixed artifact vocabulary of a saved verified-model directory. Filenames
# are fixed per role (model.mzn, data.dzn, checker.mzc.mzn, problem.md,
# solve-result.json, .openconstraint-model.json), so the role — not the
# filename — is the stable key clients branch on.
SavedArtifactRole = Literal[
    "model",
    "data",
    "checker",
    "problem",
    "solve_result",
    "solution",
    "manifest",
    "experiment_log",
    "replay_config",
]


class SavedModelArtifact(BaseModel):
    """One file written by a verified-model save.

    ``path`` is a bare filename relative to the saved directory — never an
    absolute path — matching the manifest's artifact convention. ``sha256`` is
    the hex digest of the file's bytes as written to disk.
    """

    role: SavedArtifactRole
    path: str
    sha256: str


class SaveVerifiedModelResult(BaseModel):
    """Outcome of a save-verified-model request.

    ``status="saved"`` means the server re-verified the supplied artifacts
    through the managed runtime (clean check, satisfied/optimal solve, checker
    ``completed`` when one was supplied) and wrote the artifact directory.
    ``status="not_verified"`` means a verification gate failed and nothing was
    written — ``target_dir`` is echoed for both outcomes, so on
    ``not_verified`` it names the directory that was *not* written. ``check``
    is always present (the compile gate runs first); ``solve`` is ``None``
    exactly when that check gate failed and no solve ran. ``files`` defaults
    to empty and is populated only on ``status="saved"``.
    """

    status: SaveStatus
    message: str
    target_dir: str
    files: list[SavedModelArtifact] = Field(default_factory=list)
    check: CheckResult
    solve: SolveResult | None = None


# MiniZinc 2.9.7 `--model-interface-only` base-type vocabulary, verified against
# the managed binary. `set of`/array/`opt` are reported as MODIFIERS on a base
# type (`set`/`dim`/`optional`), not as their own tags, so they are absent here;
# an enum collapses to "int" (the enum name lives only in `--model-types-only`).
# `tuple`/`record` get their own tag with no component breakdown in this mode.
# `ann` is MiniZinc's annotation type (e.g. a `seq_search` strategy list); like
# any base type it can carry `dim`, so `array of ann` reports `ann` at dim 1.
InterfaceBaseType = Literal["int", "bool", "float", "string", "tuple", "record", "ann"]
# MiniZinc's "method" value — the solve kind, directly from the interface output.
SolveMethod = Literal["sat", "min", "max"]
InspectStatus = Literal[
    "ok",  # rc == 0 — the interface was extracted (NOT a data-completeness signal)
    "error",  # rc != 0, or rc 0 with unparseable interface output (see stderr)
    "timeout",  # subprocess wall-clock cap fired during type analysis
]


class InterfaceType(BaseModel):
    """One model-interface entry's type, mapped from MiniZinc's interface JSON.

    Plain public fields with NO Pydantic aliases: ``parse_model_interface`` maps
    MiniZinc's raw ``type``/``set``/``dim``/``optional`` keys onto these names as it
    builds each entry, so both the advertised ``outputSchema`` and the emitted
    ``structuredContent`` use ``base_type``/``is_set``/``is_optional`` and cannot
    disagree.
    """

    base_type: InterfaceBaseType
    dim: int = 0  # array dimensionality (0 = scalar)
    is_set: bool = False
    is_optional: bool = False  # MiniZinc `opt` type (reported as "optional": true)


class ModelInterface(BaseModel):
    """A MiniZinc model's interface, extracted without solving.

    ``required_parameters`` are the parameters still needing a value given any
    data supplied (MiniZinc's ``input``); an empty map means the data is
    complete. ``output_variables`` (MiniZinc's ``output``) are advisory — with an
    ``output`` item they track the model's output variables and exclude
    functionally-defined vars; treat them as "the model's output variables", not
    "every decision variable".
    """

    method: SolveMethod
    required_parameters: dict[str, InterfaceType]
    output_variables: dict[str, InterfaceType]
    has_output_item: bool
    globals: list[str] = Field(default_factory=list)
    included_files: list[str] = Field(default_factory=list)


class ModelInspectionResult(BaseModel):
    """Outcome of inspecting a MiniZinc model's interface (no solving).

    ``status="ok"`` means only that the interface was *extracted* — it is NOT a
    data-completeness signal. A no-data inspection is ``ok`` with a non-empty
    ``required_parameters`` (the whole point of the tool); completeness is
    signalled solely by ``interface.required_parameters == {}``. ``interface`` is
    populated only when ``status == "ok"``.
    """

    status: InspectStatus
    solver: str
    interface: ModelInterface | None = None
    stdout: str
    stderr: str
    elapsed_ms: int


class UnsatCoreConstraint(BaseModel):
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    source: str


UnsatCoreStatus = Literal["mus_found", "no_core", "error", "timeout"]


class UnsatCoreResult(BaseModel):
    """Outcome of a findMUS (org.minizinc.findmus) run.

    `core` reports *a* minimal unsatisfiable subset (MUS): constraints that
    are jointly unsatisfiable and from which none can be removed without
    losing unsatisfiability. "Minimal" does NOT mean globally smallest — a
    model may have several MUSes of differing sizes and findMUS returns one.
    `stdout` holds findMUS's raw output and is authoritative; `core` is a
    best-effort structured view and may be empty even when status is
    "mus_found". `no_core` means findMUS finished without reporting a MUS,
    NOT that the model is satisfiable — a tight `timeout_ms` can also surface
    as `no_core` (findMUS may stop at its own --time-limit with rc 0).
    Clients branch on `status`; there is no derived `core_found` flag.
    """

    status: UnsatCoreStatus
    core: list[UnsatCoreConstraint] = Field(
        default_factory=list,
        description=(
            "Best-effort structured view of the minimal unsatisfiable subset "
            "(MUS) — minimal, not necessarily globally smallest. May be empty "
            'even when status is "mus_found"; stdout is the authoritative output.'
        ),
    )
    message: str
    stdout: str
    stderr: str
    elapsed_ms: int


class InstallConfig(BaseModel):
    runtime_dir: str

    @field_validator("runtime_dir")
    @classmethod
    def _runtime_dir_must_be_absolute(cls, value: str) -> str:
        if not value or not Path(value).is_absolute():
            raise ValueError("runtime_dir must be a non-empty absolute path")
        return value


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


class CpsatCheckerReport(BaseModel):
    """Result of running an optional checker script against the CP-SAT solution.

    ``status`` is the normalized server verdict: ``accepted`` only when the
    checker returned ``accepted`` with an empty ``errors`` list; ``rejected``
    when the checker rejected; ``timeout`` on a wall-clock timeout;
    ``error`` for malformed output, nonzero exit, truncation, or
    ``accepted``+non-empty-errors (self-contradictory output).
    ``stdout``, ``stderr``, and ``details`` are raw checker output and are
    NOT persisted in the manifest (only the scalar summary is saved).
    """

    status: Literal["accepted", "rejected", "error", "timeout"]
    errors: list[str]
    details: dict | None = None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


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
