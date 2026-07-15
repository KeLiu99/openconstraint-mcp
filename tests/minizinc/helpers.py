from __future__ import annotations

import json
import subprocess
from typing import Any

# Shared `--json-stream` test fixtures for the MiniZinc solve/checker parsers and
# the MCP tools that wrap them. The pinned managed runtime (MiniZinc 2.9.7) emits
# one JSON object per line; these builders serialize the exact shapes the parsers
# consume, so the captured format lives in one place instead of drifting across
# every test module. The parsers read only `output.default` and `output.json`
# (never `sections`/`raw`), so the richer keys here document the real transcript
# without affecting any parse result. Names are public (no leading underscore):
# this is a shared support module, so its exports are its API — same convention as
# the fixtures in `tests/conftest.py`.


class FakeCompletedProcess:
    """Stand-in for the ``subprocess.Popen`` handle the MiniZinc runner drives.

    ``_invoke_minizinc`` launches via the shared ``popen_process_group`` launcher
    and reads the result through ``communicate``/``returncode`` (a ``Popen`` handle
    rather than ``subprocess.run`` so the live child can be registered with the
    teardown tracker). Tests patch ``core.popen_process_group`` to return one of
    these; it exposes exactly what the runner touches: ``communicate`` (returning
    the captured ``stdout``/``stderr``), ``returncode``, ``poll`` (non-``None`` so
    a tree-kill on this fake is a no-op), ``pid``, and the context-manager protocol.

    With ``timeout=True`` the first ``communicate`` raises ``TimeoutExpired`` (the
    runner then tree-kills and drains a second ``communicate``, which returns the
    partial ``stdout``) — the way a real wall-clock-cap firing is exercised. The
    timeout passed to ``communicate`` is recorded on ``communicate_timeout`` (and,
    when the recording helpers attach a call record, into that record) so a test
    can assert the outer grace window.
    """

    def __init__(self, stdout: str, stderr: str, returncode: int, *, timeout: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 4321
        self._timeout = timeout
        self._communicate_calls = 0
        self.communicate_timeout: float | None = None
        self._record: dict[str, Any] | None = None

    def poll(self) -> int | None:
        # Non-None → terminate_process_tree treats the fake as already-exited, so
        # the timeout path never reaches os.getpgid/killpg on a fake pid.
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self._communicate_calls += 1
        self.communicate_timeout = timeout
        if self._record is not None:
            self._record["communicate_timeout"] = timeout
        if self._timeout and self._communicate_calls == 1:
            raise subprocess.TimeoutExpired(
                cmd="minizinc", timeout=timeout or 0, output=self.stdout, stderr=self.stderr
            )
        return self.stdout, self.stderr

    def __enter__(self) -> FakeCompletedProcess:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def stream(*objects: dict[str, Any]) -> str:
    """Serialize objects as a ``--json-stream`` transcript: one JSON object per line."""
    return "".join(json.dumps(obj) + "\n" for obj in objects)


def solution_obj(default: str, values: dict[str, Any]) -> dict[str, Any]:
    """A ``{"type":"solution"}`` object carrying both the human ``default`` text and
    the ``json`` variable map — the shape a model with an explicit ``output`` item emits."""
    return {
        "type": "solution",
        "output": {"default": default, "raw": default, "json": values},
        "sections": ["default", "raw", "json"],
    }


def solution_obj_json_only(values: dict[str, Any]) -> dict[str, Any]:
    """A solution object from a model with no explicit ``output`` item: under
    ``--output-mode json`` only the ``json`` section is present, so the human stdout
    has to be synthesized from the variable map."""
    return {"type": "solution", "output": {"json": values}, "sections": ["json"]}


VIOLATION_DIAGNOSTIC = "model inconsistency detected: expression evaluated to false"


def checker_pass(default_text: str) -> dict[str, Any]:
    """A clean ``--solution-checker`` verdict: the author CORRECT/INCORRECT text is
    surfaced verbatim from the checker's top-level ``output.default`` (the pinned
    runtime hoists it there). The documented nested-only shape is exercised
    separately by the checker parser's own tests."""
    return {
        "type": "checker",
        "messages": [{"type": "solution", "output": {"default": default_text}}],
        "output": {"default": default_text},
    }


def checker_violation() -> dict[str, Any]:
    """A constraint-style rejection: a nested ``status: UNSATISFIABLE`` (the one
    machine-readable "invalid solution" signal) plus a warning diagnostic, and no
    top-level rendered output."""
    return {
        "type": "checker",
        "messages": [
            {"type": "warning", "message": VIOLATION_DIAGNOSTIC},
            {"type": "status", "status": "UNSATISFIABLE"},
        ],
    }


# Canonical assembled `--json-stream` solve transcripts. They are shared by the
# stream parser's own tests and by the solve_model orchestration tests that feed
# them as a fake `subprocess` stdout, so the captured shapes live here once rather
# than being rebuilt (or drifting) in each module.

# Optimization proven optimal: one solution, OPTIMAL_SOLUTION, then statistics.
STREAM_OPTIMAL = stream(
    {"type": "statistics", "statistics": {"method": "maximize", "flatTime": 0.04}},
    solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
    {"type": "status", "status": "OPTIMAL_SOLUTION"},
    {"type": "statistics", "statistics": {"nSolutions": 1}},
    {"type": "statistics", "statistics": {"objective": 22, "failures": 0, "solveTime": 0.0005}},
)

# Optimization with `-a`: one solution per improving step (objectives 0, 4, 22),
# then OPTIMAL_SOLUTION. `solution` is the last/best element.
STREAM_OPTIMAL_MULTI = stream(
    solution_obj("x=0 y=0 total=0\n", {"x": 0, "y": 0, "_objective": 0}),
    solution_obj("x=0 y=2 total=4\n", {"x": 0, "y": 2, "_objective": 4}),
    solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
    {"type": "status", "status": "OPTIMAL_SOLUTION"},
    {"type": "statistics", "statistics": {"nSolutions": 3, "objective": 22}},
)

# A single `satisfy` solve: a solution and statistics, but NO status object —
# search stops at the first solution, so there is no completeness verdict.
STREAM_SATISFY = stream(
    {"type": "statistics", "statistics": {"method": "satisfy", "flatTime": 0.04}},
    solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    {"type": "statistics", "statistics": {"nSolutions": 1}},
)

# A `satisfy` solve with `-a`: every solution in order, then ALL_SOLUTIONS.
STREAM_SATISFY_ALL = stream(
    solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    solution_obj("x=1 y=3\n", {"x": 1, "y": 3}),
    solution_obj("x=2 y=3\n", {"x": 2, "y": 3}),
    {"type": "status", "status": "ALL_SOLUTIONS"},
    {"type": "statistics", "statistics": {"nSolutions": 3}},
)

# UNSAT: an optional warning, statistics, then UNSATISFIABLE and no solution.
STREAM_UNSAT = stream(
    {"type": "warning", "message": "model inconsistency detected"},
    {"type": "statistics", "statistics": {"method": "satisfy", "flatTime": 0.04}},
    {"type": "status", "status": "UNSATISFIABLE"},
)

# A syntax/compile error: a single error object on the stdout stream (the real
# process stderr stays empty), and no status object.
STREAM_ERROR = stream(
    {
        "type": "error",
        "what": "syntax error",
        "location": {"filename": "model.mzn", "firstLine": 2},
        "message": "unexpected item, expecting ';' or end of file",
    }
)

# A findMUS run over an over-constrained model, with the noisy preamble the real
# binary emits (FznSubProblem/Brief lines) plus a MUS line and pipe-delimited
# trace spans — two from the entry model and one from an included file. Shared by
# the unsat-core parser tests and the find_unsat_core orchestration tests.
UNSAT_CORE_MODEL = (
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "\n"
    "constraint x + y > 5;\n"
    "constraint x + y < 3;\n"
    "constraint x != y;\n"
    "\n"
    "solve satisfy;\n"
)

UNSAT_CORE_STDOUT = (
    "FznSubProblem:  hard cons: 0    soft cons: 3   leaves: 3      "
    "branches: 4    Built tree in 0.01 seconds.\n"
    "MUS: 1 2\n"
    "Brief: int_lin_le, int_lin_le\n"
    "Traces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;"
    "redefinitions.mzn|10|1|10|5|\n"
)
