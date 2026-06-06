from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RuntimeStatus(BaseModel):
    installed: bool
    runtime_dir: str
    minizinc_binary: str | None = None


class SolverCapabilities(BaseModel):
    # Read from the solver config's stdFlags (--solvers-json): which standard
    # solve controls the managed runtime declares for this solver. Advisory —
    # the server does not gate these at solve time.
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
