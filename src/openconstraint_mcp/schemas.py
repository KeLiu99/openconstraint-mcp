from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RuntimeStatus(BaseModel):
    installed: bool
    runtime_dir: str
    minizinc_binary: str | None = None


class SolverInfo(BaseModel):
    id: str
    name: str
    version: str | None = None
    tags: list[str] = Field(default_factory=list)


class SolverList(BaseModel):
    solvers: list[SolverInfo]


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
    stdout: str
    stderr: str
    elapsed_ms: int


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
