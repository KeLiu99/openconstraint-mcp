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
    MiniZincExecutionError,
    _parse_status,
    check_model,
    list_solvers,
    solve_model,
)
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas import CheckResult, SolveResult, SolverList, SolveStatus


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
        stdout="x = 3;\n----------\n==========\n",
        stderr="",
        elapsed_ms=42,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "optimal",
        "solver": "cp-sat",
        "stdout": "x = 3;\n----------\n==========\n",
        "stderr": "",
        "elapsed_ms": 42,
    }


def test_solve_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SolveResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
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


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _record_subprocess(
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to record args/kwargs and return ``completed``.

    Captures the cmd, kwargs, model-file existence, and model-file contents at
    the moment ``subprocess.run`` was called — solve_model deletes the temp dir
    on return, so post-call existence checks would race the cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        cmd = args[0]
        model_path = Path(cmd[-1])
        calls.append(
            {
                "args": args,
                "kwargs": kwargs,
                "model_path_existed": model_path.is_file(),
                "model_contents": (model_path.read_text() if model_path.is_file() else None),
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

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


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

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


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
