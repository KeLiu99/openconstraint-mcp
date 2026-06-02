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
    _parse_statistics,
    _parse_status,
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
    result = SolveResult(
        status="optimal",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x = 3;\n----------\n==========\n",
        stderr="",
        elapsed_ms=42,
        # Raw value tokens: a numeric stat and a quoted-string stat whose
        # literal quotes are kept verbatim, not coerced/stripped.
        statistics={"failures": "19", "method": '"satisfy"'},
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "optimal",
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        "stdout": "x = 3;\n----------\n==========\n",
        "stderr": "",
        "elapsed_ms": 42,
        "statistics": {"failures": "19", "method": '"satisfy"'},
    }


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


@pytest.mark.parametrize(
    ("stdout", "returncode", "timed_out", "expected"),
    [
        ("=====ERROR=====\n=====UNSATISFIABLE=====\n", 0, True, "timeout"),
        ("=====ERROR=====\n=====UNSATISFIABLE=====\n", 0, False, "error"),
        ("=====UNSATISFIABLE=====\n", 0, False, "unsatisfiable"),
        ("=====UNBOUNDED=====\n", 0, False, "unbounded"),
        ("=====UNSATorUNBOUNDED=====\n", 0, False, "unsat_or_unbounded"),
        ("=====UNKNOWN=====\n", 0, False, "unknown"),
        ("x = 3;\n----------\n==========\n", 0, False, "optimal"),
        ("==========\n", 0, False, "optimal"),
        ("x = 3;\n----------\n", 0, False, "satisfied"),
        ("", 1, False, "error"),
        ("", 0, False, "unknown"),
    ],
)
def test_parse_status_follows_precedence_table(
    stdout: str,
    returncode: int,
    timed_out: bool,
    expected: SolveStatus,
) -> None:
    assert _parse_status(stdout, returncode, timed_out) == expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        # A rule of more than ten equals printed by the model's own output
        # block must not be read as the optimality marker, which is exactly
        # ten equals alone on its own line.
        ("x = 3;\n=============\n----------\n", "satisfied"),
        # A status string embedded in a longer output line is not a marker;
        # FlatZinc markers always occupy their own line.
        ("x = 2; note =====UNKNOWN===== ref\n----------\n", "satisfied"),
    ],
)
def test_parse_status_ignores_markers_embedded_in_output_lines(
    stdout: str,
    expected: SolveStatus,
) -> None:
    assert _parse_status(stdout, returncode=0, timed_out=False) == expected


# A representative `--statistics` stdout: a compile/flatten block before the
# solution and a solve block after the `----------` marker, each closed by the
# `%%%mzn-stat-end` sentinel (verified shape on MiniZinc 2.9.7 / cp-sat).
_STATS_STDOUT = (
    "% Generated FlatZinc statistics:\n"
    "%%%mzn-stat: flatIntVars=8\n"
    '%%%mzn-stat: method="satisfy"\n'
    "%%%mzn-stat: flatTime=0.0652244\n"
    "%%%mzn-stat-end\n"
    "q = [1, 5, 8, 6, 3, 7, 2, 4]\n"
    "----------\n"
    "%%%mzn-stat: failures=19\n"
    "%%%mzn-stat: solveTime=0.0107676\n"
    "%%%mzn-stat: nSolutions=1\n"
    "%%%mzn-stat-end\n"
)


def test_parse_statistics_extracts_pairs_from_blocks() -> None:
    # Both blocks contribute; the human "% Generated..." comment and the
    # `%%%mzn-stat-end` sentinel are not keys, and the quoted-string value keeps
    # its literal quotes (raw token, not coerced).
    assert _parse_statistics(_STATS_STDOUT) == {
        "flatIntVars": "8",
        "method": '"satisfy"',
        "flatTime": "0.0652244",
        "failures": "19",
        "solveTime": "0.0107676",
        "nSolutions": "1",
    }


def test_parse_statistics_without_stat_lines_returns_empty() -> None:
    assert _parse_statistics("x = 3;\n----------\n==========\n") == {}


def test_parse_statistics_skips_line_without_equals() -> None:
    # A timeout can cut a block mid-line: `%%%mzn-stat: solveTi` has no `=`, so
    # it must be dropped rather than recorded as a phantom empty-valued key.
    stats = _parse_statistics("%%%mzn-stat: failures=19\n%%%mzn-stat: solveTi\n")
    assert stats == {"failures": "19"}
    assert "solveTi" not in stats


def test_parse_statistics_duplicate_key_last_wins() -> None:
    # Optimization re-emits `objective=` per improved solution; the flat dict
    # keeps only the last value while raw stdout retains the full trail.
    stats = _parse_statistics(
        "%%%mzn-stat: objective=10\n%%%mzn-stat: objective=7\n%%%mzn-stat: objective=4\n"
    )
    assert stats == {"objective": "4"}


def test_parse_statistics_includes_model_printed_lookalike() -> None:
    # stdout is one unauthenticated stream, so a model `output` block can print
    # a stat-shaped line. The positional parser includes it — pinned here as
    # documented behavior, not defended against.
    stats = _parse_statistics(
        "%%%mzn-stat: solveTime=0.01\n"
        "%%%mzn-stat-end\n"
        '%%%mzn-stat: injected="by-model-output"\n'
        "----------\n"
    )
    assert stats["injected"] == '"by-model-output"'


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


def test_solve_model_happy_path_returns_optimal(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "x = 3;\n----------\n=========="
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout=stdout, stderr="", returncode=0)
    )

    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert isinstance(result, SolveResult)
    assert result.status == "optimal"
    assert result.solver == "cp-sat"
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.stdout == stdout
    assert result.stderr == ""
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_solve_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    solve_model(model)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]

    assert cmd[0] == str(fake_minizinc_binary)
    solver_idx = cmd.index("--solver")
    assert cmd[solver_idx + 1] == "cp-sat"
    timeout_idx = cmd.index("--time-limit")
    assert cmd[timeout_idx + 1] == "30000"
    # Solve runs request statistics so the result can surface them.
    assert "--statistics" in cmd

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


def test_solve_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    result = solve_model(model, data="n = 3;")

    cmd = calls[0]["args"][0]
    # Canonical MiniZinc order: model file, then data file as the last argument.
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["model_contents"] == model
    assert calls[0]["data_contents"] == "n = 3;"
    assert result.status == "optimal"


def test_solve_model_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
        _FakeCompletedProcess(stdout="=====UNSATISFIABLE=====\n", stderr="", returncode=0),
    )

    result = solve_model("constraint false;\nsolve satisfy;")

    assert result.status == "unsatisfiable"


@pytest.mark.parametrize(
    ("stdout", "expected_status"),
    [
        pytest.param(
            "% Generated FlatZinc statistics:\n"
            '%%%mzn-stat: method="satisfy"\n'
            "%%%mzn-stat-end\n"
            "x = 3;\n"
            "----------\n"
            "==========\n"
            "%%%mzn-stat: solveTime=0.01\n"
            "%%%mzn-stat-end\n",
            "optimal",
            id="optimal-with-stats",
        ),
        pytest.param(
            "% Generated FlatZinc statistics:\n"
            "%%%mzn-stat: flatIntVars=2\n"
            "%%%mzn-stat-end\n"
            "=====UNSATISFIABLE=====\n",
            "unsatisfiable",
            id="unsat-with-stats",
        ),
    ],
)
def test_solve_model_status_unaffected_by_stat_lines(
    stdout: str,
    expected_status: SolveStatus,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The injected `%`-comment stat lines must not collide with _parse_status's
    # whole-line marker matching: status is classified from the FlatZinc markers
    # exactly as without --statistics, and the stat block is still captured.
    _record_subprocess(monkeypatch, _FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("solve satisfy;")

    assert result.status == expected_status
    assert result.statistics  # the stat block was parsed despite the markers


def test_solve_model_tolerates_truncated_stat_block(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A truncated final stat line (no `=`) must not raise, must not inject a
    # phantom key, and must not change the classified status.
    stdout = (
        "x = 3;\n"
        "----------\n"
        "==========\n"
        "%%%mzn-stat: failures=19\n"
        "%%%mzn-stat: solveTi"  # truncated mid-line, no '='
    )
    _record_subprocess(monkeypatch, _FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("solve satisfy;")

    assert isinstance(result, SolveResult)
    assert result.status == "optimal"
    assert result.statistics == {"failures": "19"}
    assert "solveTi" not in result.statistics


def test_solve_model_timeout_with_bytes_payload_decodes(
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

    result = solve_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.return_code is None
    assert result.timed_out is True
    assert result.stdout == "partial"
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
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
    # Statistics is solve-only; a compile-check must not request it.
    assert "--statistics" not in cmd

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
    # Statistics is solve-only; the findMUS path must not request it.
    assert "--statistics" not in cmd
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
