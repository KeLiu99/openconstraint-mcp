from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openconstraint_mcp.minizinc import (
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    FINDMUS_SOLVER,
    MiniZincExecutionError,
    _parse_solve_stream,
    _parse_unsat_core,
    _slice_source,
    check_model,
    find_unsat_core,
    list_solvers,
    solve_model,
)
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas import (
    CheckResult,
    SolveResult,
    SolverList,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
)

_UNSAT_CORE_MODEL = (
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "\n"
    "constraint x + y > 5;\n"
    "constraint x + y < 3;\n"
    "constraint x != y;\n"
    "\n"
    "solve satisfy;\n"
)

_UNSAT_CORE_STDOUT = (
    "FznSubProblem:  hard cons: 0    soft cons: 3   leaves: 3      "
    "branches: 4    Built tree in 0.01 seconds.\n"
    "MUS: 1 2\n"
    "Brief: int_lin_le, int_lin_le\n"
    "Traces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;"
    "redefinitions.mzn|10|1|10|5|\n"
)


def test_list_solvers_raises_clear_error_when_runtime_missing(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        list_solvers()
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_list_solvers_parses_solvers_json(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        [
            {
                "id": "org.gecode.gecode",
                "name": "Gecode",
                "version": "6.3.0",
                "tags": ["cp", "int"],
            },
            {
                "id": "com.google.or-tools.cpsat",
                "name": "OR-Tools CP-SAT",
                "version": "9.10",
            },
        ]
    )

    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(payload)

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = list_solvers()
    assert isinstance(result, SolverList)
    assert [solver.id for solver in result.solvers] == [
        "org.gecode.gecode",
        "com.google.or-tools.cpsat",
    ]
    assert result.solvers[0].tags == ["cp", "int"]
    assert result.solvers[1].version == "9.10"
    assert result.solvers[1].tags == []


def test_list_solvers_wraps_subprocess_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[str(fake_minizinc_binary), "--solvers-json"],
            stderr="MiniZinc: invalid solver configuration\n",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        list_solvers()
    message = str(exc_info.value)
    assert "invalid solver configuration" in message
    assert "install-runtime" in message


def test_list_solvers_wraps_exec_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        list_solvers()
    assert "install-runtime" in str(exc_info.value)


def test_list_solvers_decodes_output_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCompleted:
        stdout = "[]"
        returncode = 0

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompleted:
        calls.append({"kwargs": kwargs})
        return _FakeCompleted()

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    list_solvers()

    assert calls[0]["kwargs"].get("encoding") == "utf-8"


def test_solve_result_round_trips() -> None:
    # A multi-solution optimization result: `solutions` holds the improving
    # sequence in order, `solution` is its last (best) element, `objective` is
    # the best `_objective`, and `statistics` are bare stringified stream values
    # (no raw-token quotes, unlike the old %%%mzn-stat: scrape).
    result = SolveResult(
        status="optimal",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=0 y=1 total=2\nx=2 y=10 total=22\n",
        stderr="",
        elapsed_ms=42,
        statistics={"failures": "0", "method": "maximize"},
        solution={"x": 2, "y": 10},
        solutions=[{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        objective=22,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "optimal",
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        "stdout": "x=0 y=1 total=2\nx=2 y=10 total=22\n",
        "stderr": "",
        "elapsed_ms": 42,
        "statistics": {"failures": "0", "method": "maximize"},
        "solution": {"x": 2, "y": 10},
        "solutions": [{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        "objective": 22,
    }


def test_solve_result_round_trips_satisfaction_has_null_objective() -> None:
    # A satisfaction result carries a solution but no objective: `_objective` is
    # absent from a satisfy model's json section, so `objective` stays None while
    # `solution`/`solutions` still round-trip.
    result = SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=1 y=2\n",
        stderr="",
        elapsed_ms=10,
        solution={"x": 1, "y": 2},
        solutions=[{"x": 1, "y": 2}],
        objective=None,
    )
    dumped = result.model_dump()
    assert dumped["objective"] is None
    assert dumped["solution"] == {"x": 1, "y": 2}
    assert dumped["solutions"] == [{"x": 1, "y": 2}]
    assert dumped["status"] == "satisfied"


def test_solve_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SolveResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_check_result_round_trips() -> None:
    result = CheckResult(
        status="ok",
        solver="cp-sat",
        stdout="",
        stderr="",
        elapsed_ms=12,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "ok",
        "solver": "cp-sat",
        "stdout": "",
        "stderr": "",
        "elapsed_ms": 12,
    }


def test_check_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CheckResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_unsat_core_result_round_trips() -> None:
    result = UnsatCoreResult(
        status="mus_found",
        core=[
            UnsatCoreConstraint(
                line=4,
                column=12,
                end_line=4,
                end_column=20,
                source="x + y > 5",
            )
        ],
        message="findMUS reported a minimal unsatisfiable subset.",
        stdout="MUS: 1 2\n",
        stderr="",
        elapsed_ms=7,
    )

    assert result.model_dump() == {
        "status": "mus_found",
        "core": [
            {
                "line": 4,
                "column": 12,
                "end_line": 4,
                "end_column": 20,
                "source": "x + y > 5",
            }
        ],
        "message": "findMUS reported a minimal unsatisfiable subset.",
        "stdout": "MUS: 1 2\n",
        "stderr": "",
        "elapsed_ms": 7,
    }


def test_unsat_core_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        UnsatCoreResult(
            status="bogus",  # type: ignore[arg-type]
            message="bad status",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_parse_unsat_core_extracts_model_spans() -> None:
    mus_present, core = _parse_unsat_core(_UNSAT_CORE_STDOUT, _UNSAT_CORE_MODEL)

    assert mus_present is True
    assert len(core) == 2
    assert core[0].line == 4
    assert core[0].column == 12
    assert core[0].end_line == 4
    assert core[0].end_column == 20
    assert "x + y > 5" in core[0].source
    assert "x + y < 3" in core[1].source
    assert all("x != y" not in item.source for item in core)


def test_parse_unsat_core_without_mus_returns_empty_core() -> None:
    assert _parse_unsat_core("=====UNKNOWN=====\n", _UNSAT_CORE_MODEL) == (False, [])


def test_parse_unsat_core_ignores_trace_spans_without_mus_line() -> None:
    assert _parse_unsat_core("Traces: model.mzn|4|12|4|20|\n", _UNSAT_CORE_MODEL) == (False, [])


# 1-indexed, end-inclusive spans over a 3-line model whose lines are each 5 chars.
_SLICE_MODEL = "abcde\nfghij\nklmno"


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec", "expected"),
    [
        pytest.param(1, 2, 1, 4, "bcd", id="single-line"),
        pytest.param(1, 3, 3, 2, "cde\nfghij\nkl", id="multi-line"),
    ],
)
def test_slice_source_returns_precise_span(
    sl: int, sc: int, el: int, ec: int, expected: str
) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == expected


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec"),
    [
        pytest.param(3, 1, 1, 1, id="start-after-end"),
        pytest.param(5, 1, 6, 2, id="start-past-eof"),
    ],
)
def test_slice_source_invalid_line_span_returns_empty(sl: int, sc: int, el: int, ec: int) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == ""


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec", "expected"),
    [
        pytest.param(2, 9, 2, 10, "fghij", id="column-past-line-end"),
        pytest.param(1, 4, 1, 2, "abcde", id="start-col-after-end-col"),
        pytest.param(2, 1, 5, 3, "fghij\nklmno", id="end-line-past-eof-clamped"),
    ],
)
def test_slice_source_falls_back_to_whole_lines(
    sl: int, sc: int, el: int, ec: int, expected: str
) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == expected


# ---------------------------------------------------------------------------
# Captured `--json-stream` solve transcripts (cp-sat, managed runtime). Built by
# serializing dicts so the JSON escaping is exact: one object per line, each
# solution carrying both the human `default` text and the `json` variable map,
# with status and statistics as their own sibling objects.
# ---------------------------------------------------------------------------
def _stream(*objects: dict[str, Any]) -> str:
    return "".join(json.dumps(obj) + "\n" for obj in objects)


def _solution_obj(default: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "solution",
        "output": {"default": default, "raw": default, "json": values},
        "sections": ["default", "raw", "json"],
    }


def _solution_obj_json_only(values: dict[str, Any]) -> dict[str, Any]:
    # A real model with no explicit `output` item: under `--output-mode json` the
    # solution object carries only the `json` section (no `default`/`raw`), so the
    # human stdout has to be synthesized from the variable map.
    return {"type": "solution", "output": {"json": values}, "sections": ["json"]}


# Optimization proven optimal: one solution, OPTIMAL_SOLUTION, then statistics.
_STREAM_OPTIMAL = _stream(
    {"type": "statistics", "statistics": {"method": "maximize", "flatTime": 0.04}},
    _solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
    {"type": "status", "status": "OPTIMAL_SOLUTION"},
    {"type": "statistics", "statistics": {"nSolutions": 1}},
    {"type": "statistics", "statistics": {"objective": 22, "failures": 0, "solveTime": 0.0005}},
)

# Optimization with `-a`: one solution per improving step (objectives 0, 4, 22),
# then OPTIMAL_SOLUTION. `solution` is the last/best element.
_STREAM_OPTIMAL_MULTI = _stream(
    _solution_obj("x=0 y=0 total=0\n", {"x": 0, "y": 0, "_objective": 0}),
    _solution_obj("x=0 y=2 total=4\n", {"x": 0, "y": 2, "_objective": 4}),
    _solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
    {"type": "status", "status": "OPTIMAL_SOLUTION"},
    {"type": "statistics", "statistics": {"nSolutions": 3, "objective": 22}},
)

# A single `satisfy` solve: a solution and statistics, but NO status object —
# search stops at the first solution, so there is no completeness verdict.
_STREAM_SATISFY = _stream(
    {"type": "statistics", "statistics": {"method": "satisfy", "flatTime": 0.04}},
    _solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    {"type": "statistics", "statistics": {"nSolutions": 1}},
)

# A `satisfy` solve with `-a`: every solution in order, then ALL_SOLUTIONS.
_STREAM_SATISFY_ALL = _stream(
    _solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    _solution_obj("x=1 y=3\n", {"x": 1, "y": 3}),
    _solution_obj("x=2 y=3\n", {"x": 2, "y": 3}),
    {"type": "status", "status": "ALL_SOLUTIONS"},
    {"type": "statistics", "statistics": {"nSolutions": 3}},
)

# UNSAT: an optional warning, statistics, then UNSATISFIABLE and no solution.
_STREAM_UNSAT = _stream(
    {"type": "warning", "message": "model inconsistency detected"},
    {"type": "statistics", "statistics": {"method": "satisfy", "flatTime": 0.04}},
    {"type": "status", "status": "UNSATISFIABLE"},
)

# A syntax/compile error: a single error object on the stdout stream (the real
# process stderr stays empty), and no status object.
_STREAM_ERROR = _stream(
    {
        "type": "error",
        "what": "syntax error",
        "location": {"filename": "model.mzn", "firstLine": 2},
        "message": "unexpected item, expecting ';' or end of file",
    }
)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("OPTIMAL_SOLUTION", "optimal"),
        ("ALL_SOLUTIONS", "satisfied"),
        ("SATISFIED", "satisfied"),
        ("UNSATISFIABLE", "unsatisfiable"),
        ("UNKNOWN", "unknown"),
        ("UNBOUNDED", "unbounded"),
        ("UNSAT_OR_UNBOUNDED", "unsat_or_unbounded"),
    ],
)
def test_parse_solve_stream_maps_known_status(verdict: str, expected: SolveStatus) -> None:
    # Status is read from the driver's own `{"type":"status"}` object (never from
    # text); each verified/enum spelling maps onto a SolveStatus literal.
    assert _parse_solve_stream(_stream({"type": "status", "status": verdict})).status == expected


def test_parse_solve_stream_unknown_status_falls_back_safely() -> None:
    # A renamed or newly added MiniZinc verdict never crashes a solve: with a
    # solution in hand it reads as satisfied, otherwise unknown.
    with_solution = _stream(
        _solution_obj("x=1\n", {"x": 1}),
        {"type": "status", "status": "FUTURE_VERDICT"},
    )
    without_solution = _stream({"type": "status", "status": "FUTURE_VERDICT"})
    assert _parse_solve_stream(with_solution).status == "satisfied"
    assert _parse_solve_stream(without_solution).status == "unknown"


def test_parse_solve_stream_strips_objective_and_orders_solutions() -> None:
    # `solutions` preserves emission order with `_objective` removed from each;
    # `objective` is the last (best) solution's `_objective`.
    parsed = _parse_solve_stream(_STREAM_OPTIMAL_MULTI)
    assert parsed.solutions == [{"x": 0, "y": 0}, {"x": 0, "y": 2}, {"x": 2, "y": 10}]
    assert all("_objective" not in solution for solution in parsed.solutions)
    assert parsed.objective == 22
    assert parsed.status == "optimal"


def test_parse_solve_stream_satisfaction_has_no_objective() -> None:
    # A satisfy model's json section carries no `_objective`, so objective is None
    # even though solutions are present.
    parsed = _parse_solve_stream(_STREAM_SATISFY_ALL)
    assert parsed.objective is None
    assert parsed.solutions[-1] == {"x": 2, "y": 3}
    assert parsed.status == "satisfied"


def test_parse_solve_stream_merges_statistics_last_wins() -> None:
    # Typed JSON stat values become bare strings; duplicate keys across objects
    # keep the last value (mirroring the old block-merge contract).
    stream = _stream(
        {"type": "statistics", "statistics": {"method": "maximize", "objective": 0}},
        {"type": "statistics", "statistics": {"objective": 22, "flatTime": 0.04, "failures": 0}},
    )
    assert _parse_solve_stream(stream).statistics == {
        "method": "maximize",
        "objective": "22",
        "flatTime": "0.04",
        "failures": "0",
    }


def test_parse_solve_stream_reconstructs_stdout_from_default_sections() -> None:
    # The human stdout is rebuilt from each solution's `output.default`, one
    # newline-terminated block per solution — not the raw json-stream bytes.
    assert _parse_solve_stream(_STREAM_SATISFY_ALL).stdout == "x=1 y=2\nx=1 y=3\nx=2 y=3\n"


def test_parse_solve_stream_synthesizes_stdout_when_only_json_section() -> None:
    # A model with no explicit `output` item emits a solution object carrying only
    # the `json` section. The human stdout is synthesized from the variable map
    # (with `_objective` stripped) so a real no-output solve still shows a solution
    # instead of an empty stdout.
    parsed = _parse_solve_stream(
        _stream(
            _solution_obj_json_only({"x": 5, "_objective": 5}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
    )
    assert parsed.solutions == [{"x": 5}]
    assert parsed.objective == 5
    assert parsed.stdout == "x = 5;\n"
    # The internal objective artifact never leaks into the human text.
    assert "_objective" not in parsed.stdout


def test_parse_solve_stream_skips_truncated_final_line() -> None:
    # A hard timeout can cut the final object mid-line; the unparseable tail is
    # skipped and the fully-received solution/objective are kept.
    truncated = (
        _stream(
            _solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
        + '{"type": "statistics", "statistics": {"objec'
    )
    parsed = _parse_solve_stream(truncated)
    assert parsed.status == "optimal"
    assert parsed.solutions == [{"x": 2, "y": 10}]
    assert parsed.objective == 22


def test_parse_solve_stream_surfaces_error_and_warning_messages() -> None:
    # An error object forces status "error" and its message is collected; a
    # warning contributes its message without changing the verdict.
    assert _parse_solve_stream(_STREAM_ERROR).status == "error"
    assert _parse_solve_stream(_STREAM_ERROR).messages == [
        "syntax error: unexpected item, expecting ';' or end of file"
    ]
    assert _parse_solve_stream(_STREAM_UNSAT).messages == ["model inconsistency detected"]


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _record_subprocess(
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to record args/kwargs and return ``completed``.

    Captures the cmd, kwargs, model-file existence, model-file contents, and —
    when a positional ``data.dzn`` is present — the data-file contents at the
    moment ``subprocess.run`` was called. The model and data files are located
    by suffix (``.mzn`` / ``.dzn``) rather than position, since the data file
    is appended after the model. solve_model deletes the temp dir on return,
    so post-call reads would race the cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        cmd = args[0]
        model_path = next((Path(arg) for arg in cmd if str(arg).endswith(".mzn")), Path(cmd[-1]))
        data_path = next((Path(arg) for arg in cmd if str(arg).endswith(".dzn")), None)
        data_contents: str | None = None
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        calls.append(
            {
                "args": args,
                "kwargs": kwargs,
                "model_path_existed": model_path.is_file(),
                "model_contents": (model_path.read_text() if model_path.is_file() else None),
                "data_contents": data_contents,
            }
        )
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)
    return calls


def test_solve_model_happy_path_returns_satisfied(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single `satisfy` solve emits a solution but no status object, so it is
    # classified `satisfied` from the clean exit + solution — not `optimal` (the
    # classic-`==========` misread this whole change exists to kill).
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert isinstance(result, SolveResult)
    assert result.status == "satisfied"
    assert result.solver == "cp-sat"
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.solution == {"x": 1, "y": 2}
    assert result.solutions == [{"x": 1, "y": 2}]
    assert result.objective is None
    assert result.stdout == "x=1 y=2\n"  # reconstructed from the `default` section
    assert result.stderr == ""
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_solve_model_synthesizes_human_stdout_when_model_has_no_output_item(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A satisfy model with no explicit `output` item streams only the json section.
    # The structured solution must still be paired with a human stdout block, so
    # the solution does not vanish from the model-visible text.
    stream = _stream(_solution_obj_json_only({"x": 3}))
    _record_subprocess(monkeypatch, _FakeCompletedProcess(stdout=stream, stderr="", returncode=0))

    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert result.status == "satisfied"
    assert result.solution == {"x": 3}
    assert result.solutions == [{"x": 3}]
    assert result.stdout == "x = 3;\n"


def test_solve_model_returns_structured_optimal_solution(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An optimization run: the driver's OPTIMAL_SOLUTION verdict maps to
    # `optimal`, the best objective and solution come from the last solution
    # object, and statistics are the merged, stringified stream values.
    _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_OPTIMAL, stderr="", returncode=0)
    )

    result = solve_model("var 0..10: x;\nvar 0..10: y;\nsolve maximize x + 2 * y;")

    assert result.status == "optimal"
    assert result.solution == {"x": 2, "y": 10}
    assert result.solutions == [{"x": 2, "y": 10}]
    assert result.objective == 22
    assert result.stdout == "x=2 y=10 total=22\n"
    assert result.statistics["objective"] == "22"
    assert result.statistics["solveTime"] == "0.0005"


def test_solve_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model(model)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]

    assert cmd[0] == str(fake_minizinc_binary)
    solver_idx = cmd.index("--solver")
    assert cmd[solver_idx + 1] == "cp-sat"
    timeout_idx = cmd.index("--time-limit")
    assert cmd[timeout_idx + 1] == "30000"
    # Solve runs request statistics and the json-stream transport so the result
    # can surface structured solutions, a driver-authenticated status, and stats.
    assert "--statistics" in cmd
    assert "--json-stream" in cmd
    assert "--output-objective" in cmd
    assert cmd[cmd.index("--output-mode") + 1] == "json"

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


# --- Phase 2: solver/search-control flags ----------------------------------


def _solve_cmd_with_flags(monkeypatch: pytest.MonkeyPatch, **flags: Any) -> list[str]:
    """Solve a trivial model with the given flags; return the argv it built."""
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )
    solve_model("var 1..5: x;\nsolve satisfy;", **flags)
    return calls[0]["args"][0]


def test_solve_model_free_search_adds_f_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "-f" in _solve_cmd_with_flags(monkeypatch, free_search=True)


def test_solve_model_all_solutions_adds_a_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "-a" in _solve_cmd_with_flags(monkeypatch, all_solutions=True)


def test_solve_model_parallel_adds_valued_p_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(monkeypatch, parallel=4)
    assert cmd[cmd.index("-p") + 1] == "4"


@pytest.mark.parametrize("seed", [42, 0, -5])
def test_solve_model_random_seed_adds_valued_r_flag_for_any_int(
    seed: int, fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `random_seed` accepts any int (including 0 and negatives — no validation).
    cmd = _solve_cmd_with_flags(monkeypatch, random_seed=seed)
    assert cmd[cmd.index("-r") + 1] == str(seed)


def test_solve_model_default_flags_reproduce_phase1_argv(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no flags set, the solve argv carries only the json-stream transport
    # args — none of the search-control tokens — so a default solve is identical
    # to the Phase-1 invocation.
    cmd = _solve_cmd_with_flags(monkeypatch)
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))


def test_solve_model_combined_flags_all_appear_alongside_transport(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(
        monkeypatch,
        free_search=True,
        parallel=2,
        random_seed=7,
        all_solutions=True,
    )
    assert "-f" in cmd and "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == "7"
    # The transport args are untouched by the search-control flags.
    assert "--json-stream" in cmd and "--statistics" in cmd


@pytest.mark.parametrize("bad", [0, -1])
def test_solve_model_rejects_non_positive_parallel(
    bad: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad parallel")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="parallel"):
        solve_model("solve satisfy;", parallel=bad)


def test_solve_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model(model, data="n = 3;")

    cmd = calls[0]["args"][0]
    # Canonical MiniZinc order: model file, then data file as the last argument.
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["model_contents"] == model
    assert calls[0]["data_contents"] == "n = 3;"
    assert result.status == "satisfied"


def test_solve_model_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("var 1..5: x;\nsolve satisfy;")

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_solve_model_returns_structured_error_for_malformed_data(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = (
        "Error: syntax error, unexpected ';' in file '/tmp/openconstraint-mcp-xxxx/data.dzn'\n"
    )
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout="", stderr=diagnostic, returncode=1),
    )

    result = solve_model("int: n;\nvar 1..n: x;\nsolve satisfy;", data="n = ;")

    assert result.status == "error"
    assert "data.dzn" in result.stderr
    assert "syntax error" in result.stderr


def test_solve_model_custom_solver_is_passed_through(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model("solve satisfy;", solver="gecode")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == "gecode"
    assert result.solver == "gecode"


def test_solve_model_custom_timeout_drives_outer_grace(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("solve satisfy;", timeout_ms=5000)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == "5000"
    assert calls[0]["kwargs"]["timeout"] == pytest.approx(10.0)


def test_solve_model_defaults_match_module_constants(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("solve satisfy;")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == DEFAULT_SOLVER
    assert cmd[cmd.index("--time-limit") + 1] == str(DEFAULT_SOLVE_TIMEOUT_MS)


@pytest.mark.parametrize("bad_model", ["", "\n\n  \t\n"])
def test_solve_model_rejects_empty_or_whitespace_model(
    bad_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        solve_model(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_solve_model_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        solve_model("solve satisfy;", timeout_ms=bad_timeout)


def test_solve_model_timeout_validation_precedes_runtime_check(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        solve_model("solve satisfy;", timeout_ms=0)


def test_solve_model_raises_when_runtime_missing(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        solve_model("solve satisfy;")
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_solve_model_returns_structured_result_for_minizinc_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(
            stdout="",
            stderr="MiniZinc: syntax error: unexpected token\n",
            returncode=1,
        ),
    )

    result = solve_model("solv satisfy;")

    assert result.status == "error"
    assert result.return_code == 1
    assert result.timed_out is False
    assert "syntax error" in result.stderr


def test_solve_model_returns_structured_result_for_unsatisfiable(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_STREAM_UNSAT, stderr="", returncode=0),
    )

    result = solve_model("constraint false;\nsolve satisfy;")

    assert result.status == "unsatisfiable"
    assert result.solution is None
    assert result.solutions == []
    assert result.objective is None
    # The driver routes its inconsistency warning into the stdout stream; it is
    # surfaced into the diagnostic stderr channel.
    assert "model inconsistency detected" in result.stderr


def test_solve_model_satisfy_all_returns_ordered_solutions(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `satisfy` enumeration ends in ALL_SOLUTIONS, which maps to `satisfied`
    # (not `optimal`); `solutions` keeps emission order and `solution` is the
    # last found.
    _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY_ALL, stderr="", returncode=0)
    )

    result = solve_model("solve satisfy;")

    assert result.status == "satisfied"
    assert result.solutions == [{"x": 1, "y": 2}, {"x": 1, "y": 3}, {"x": 2, "y": 3}]
    assert result.solution == {"x": 2, "y": 3}
    assert result.objective is None
    assert result.stdout == "x=1 y=2\nx=1 y=3\nx=2 y=3\n"


def test_solve_model_surfaces_stream_error_into_stderr(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `--json-stream` reports a syntax error as an `{"type":"error"}` object on
    # stdout with an empty real stderr; the runner classifies `error` and lifts
    # the message into the diagnostic stderr channel so the client can repair.
    _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_ERROR, stderr="", returncode=1)
    )

    result = solve_model("var 1..3: x\nsolve satisfy;")

    assert result.status == "error"
    assert result.solution is None
    assert "syntax error" in result.stderr
    assert "unexpected item" in result.stderr


def test_solve_model_timeout_with_bytes_payload_decodes(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hard timeout surfaces a partial json-stream as bytes; it is decoded and
    # parsed (keeping the fully-received solution), but the verdict is forced to
    # `timeout` with no real return code.
    partial = (
        _stream(_solution_obj("x=3\n", {"x": 3})) + '{"type": "stat'  # truncated tail
    ).encode("utf-8")

    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=partial,
            stderr=b"",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = solve_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.return_code is None
    assert result.timed_out is True
    assert result.solution == {"x": 3}
    assert result.stdout == "x=3\n"  # reconstructed from the partial stream
    assert result.stderr == ""
    assert result.elapsed_ms >= 0


def test_solve_model_timeout_with_none_payload_returns_empty_strings(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=None,
            stderr=None,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = solve_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.stdout == ""
    assert result.stderr == ""


def test_solve_model_wraps_oserror_as_execution_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        solve_model("solve satisfy;")
    assert "install-runtime" in str(exc_info.value)


def test_solve_model_decodes_output_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("solve satisfy;")

    assert calls[0]["kwargs"].get("encoding") == "utf-8"


def test_solve_model_writes_model_file_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    original_write_text = Path.write_text

    def _spy_write_text(self: Path, data: str, **kwargs: Any) -> int:
        captured["encoding"] = kwargs.get("encoding")
        return original_write_text(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", _spy_write_text)
    _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=_STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("% café λ\nsolve satisfy;")

    assert captured["encoding"] == "utf-8"


def test_check_model_happy_path_returns_ok(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    result = check_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert isinstance(result, CheckResult)
    assert result.status == "ok"
    assert result.solver == "cp-sat"
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_check_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    check_model(model)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]

    assert cmd[0] == str(fake_minizinc_binary)
    solver_idx = cmd.index("--solver")
    assert cmd[solver_idx + 1] == "cp-sat"
    timeout_idx = cmd.index("--time-limit")
    assert cmd[timeout_idx + 1] == "30000"
    assert "-c" in cmd
    # Statistics, the json-stream transport, and the solve-only search-control
    # flags are never requested by a compile-check.
    assert "--statistics" not in cmd
    assert "--json-stream" not in cmd
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


def test_check_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    result = check_model(model, data="n = 3;")

    cmd = calls[0]["args"][0]
    # `-c` stays present; canonical model-then-data positional order.
    assert "-c" in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "n = 3;"
    assert result.status == "ok"


def test_check_model_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    check_model("var 1..5: x;\nsolve satisfy;")

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_check_model_custom_solver_is_passed_through(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    result = check_model("solve satisfy;", solver="gecode")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == "gecode"
    assert result.solver == "gecode"


def test_check_model_custom_timeout_drives_outer_grace(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0)
    )

    check_model("solve satisfy;", timeout_ms=5000)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == "5000"
    assert calls[0]["kwargs"]["timeout"] == pytest.approx(10.0)


def test_check_model_returns_structured_result_for_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        ),
    )

    result = check_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert "type error" in result.stderr


@pytest.mark.parametrize("bad_model", ["", "\n\n  \t\n"])
def test_check_model_rejects_empty_or_whitespace_model(
    bad_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        check_model(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_check_model_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        check_model("solve satisfy;", timeout_ms=bad_timeout)


def test_check_model_timeout_validation_precedes_runtime_check(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        check_model("solve satisfy;", timeout_ms=0)


def test_check_model_raises_when_runtime_missing(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        check_model("solve satisfy;")
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_check_model_timeout_with_bytes_payload_decodes(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=b"partial",
            stderr=b"",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = check_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.stdout == "partial"
    assert result.stderr == ""
    assert result.elapsed_ms >= 0


def test_check_model_wraps_oserror_as_execution_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        check_model("solve satisfy;")
    assert "install-runtime" in str(exc_info.value)


def test_find_unsat_core_mus_found_preserves_raw_output(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(_UNSAT_CORE_MODEL)

    assert result.status == "mus_found"
    assert len(result.core) == 2
    assert result.stdout == _UNSAT_CORE_STDOUT
    assert "minimal unsatisfiable subset" in result.message


def test_find_unsat_core_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(_UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]
    solver_idx = cmd.index("--solver")
    timeout_idx = cmd.index("--time-limit")
    model_path = Path(cmd[-1])

    assert cmd[solver_idx + 1] == FINDMUS_SOLVER
    assert cmd[timeout_idx + 1] == str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)
    assert "-c" not in cmd
    # Statistics, the json-stream transport, and the solve-only search-control
    # flags are never requested by the findMUS path.
    assert "--statistics" not in cmd
    assert "--json-stream" not in cmd
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == _UNSAT_CORE_MODEL
    assert kwargs["cwd"] == str(model_path.parent)


def test_find_unsat_core_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(_UNSAT_CORE_MODEL, data="lo = 5;")

    cmd = calls[0]["args"][0]
    # findMUS only accepts a positional data file, never the --data flag.
    assert "--data" not in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "lo = 5;"
    assert cmd[cmd.index("--solver") + 1] == FINDMUS_SOLVER
    assert "-c" not in cmd
    # The structured core still resolves from the model text (model-only spans).
    assert result.status == "mus_found"
    assert len(result.core) == 2
    assert "x + y > 5" in result.core[0].source


def test_find_unsat_core_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(_UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_find_unsat_core_no_core_clears_structured_core(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout="=====UNKNOWN=====\n", stderr="", returncode=0),
    )

    result = find_unsat_core(_UNSAT_CORE_MODEL)

    assert result.status == "no_core"
    assert result.core == []


def test_find_unsat_core_error_clears_structured_core_and_preserves_stderr(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "Error: cannot load solver org.minizinc.findmus\n"
    _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout="", stderr=stderr, returncode=1),
    )

    result = find_unsat_core(_UNSAT_CORE_MODEL)

    assert result.status == "error"
    assert result.core == []
    assert result.stderr == stderr


def test_find_unsat_core_timeout_with_bytes_payload_decodes(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=b"partial",
            stderr=b"",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    result = find_unsat_core(_UNSAT_CORE_MODEL)

    assert result.status == "timeout"
    assert result.core == []
    assert result.stdout == "partial"


@pytest.mark.parametrize("bad_model", ["", "\n  \t"])
def test_find_unsat_core_rejects_empty_or_whitespace_model(
    bad_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        find_unsat_core(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_find_unsat_core_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        find_unsat_core(_UNSAT_CORE_MODEL, timeout_ms=bad_timeout)


def test_find_unsat_core_raises_when_runtime_missing(fake_runtime_dir: Path) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        find_unsat_core(_UNSAT_CORE_MODEL)
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_find_unsat_core_wraps_oserror_as_execution_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        find_unsat_core(_UNSAT_CORE_MODEL)
    assert "install-runtime" in str(exc_info.value)


def test_find_unsat_core_uses_default_timeout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        _FakeCompletedProcess(stdout=_UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(_UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)
