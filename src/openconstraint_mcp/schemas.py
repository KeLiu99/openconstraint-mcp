from __future__ import annotations

from pydantic import BaseModel, Field


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
