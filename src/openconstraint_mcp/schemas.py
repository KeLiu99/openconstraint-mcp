from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator


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
    """

    status: PortfolioStatus
    winner_index: int | None = None
    winner: SolveResult | None = None
    attempts: list[PortfolioAttempt] = Field(default_factory=list)
    elapsed_ms: int
    selection_policy: str

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
SavedArtifactRole = Literal["model", "data", "checker", "problem", "solve_result", "manifest"]


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
