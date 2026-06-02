from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc import (
    FINDMUS_SOLVER,
    MiniZincExecutionError,
    _validate_model_data_paths,
    check_model_path,
    find_unsat_core_path,
    solve_model_path,
)
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas import CheckResult, SolveResult, UnsatCoreResult

_MODEL_SRC = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"

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


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _record_run(
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to record the model/data args and cwd at call time.

    File tools run the real on-disk model, so the recorded model/data args are
    the caller's resolved paths. Args are located by ``.mzn`` / ``.dzn`` suffix.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        cmd = args[0]
        model_arg = next((arg for arg in cmd if str(arg).endswith(".mzn")), cmd[-1])
        data_arg = next((arg for arg in cmd if str(arg).endswith(".dzn")), None)
        calls.append(
            {
                "cmd": list(cmd),
                "kwargs": kwargs,
                "model_arg": str(model_arg),
                "data_arg": str(data_arg) if data_arg is not None else None,
            }
        )
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)
    return calls


def _fail_if_run_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fail)


# --- CLI-style execution (real path, cwd = model's parent) -----------------


def test_solve_runs_real_path_from_parent(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    result = solve_model_path(model_path)

    assert isinstance(result, SolveResult)
    assert result.status == "optimal"
    # The path solve runner requests statistics too, symmetric with the inline
    # solve_model (check/findMUS path runs assert its absence below).
    assert "--statistics" in calls[0]["cmd"]
    # The managed binary ran on the resolved REAL path with cwd = its parent,
    # so a relative include resolves against the model's own directory.
    assert calls[0]["model_arg"] == str(model_path.resolve())
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_relative_input_is_resolved_without_double_counting(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "model.mzn").write_text(_MODEL_SRC)
    monkeypatch.chdir(tmp_path)
    calls = _record_run(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    # Relative input: cwd=parent + relative argv would make the subprocess look
    # under sub/sub/. Resolving up front pins both to the absolute path/parent.
    solve_model_path(Path("sub/model.mzn"))

    resolved = (tmp_path / "sub" / "model.mzn").resolve()
    assert calls[0]["model_arg"] == str(resolved)
    assert calls[0]["kwargs"]["cwd"] == str(resolved.parent)


def test_data_is_positional_after_model(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text("int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;\n")
    data_path = tmp_path / "params.dzn"
    data_path.write_text("n = 3;\n")
    calls = _record_run(
        monkeypatch, _FakeCompletedProcess(stdout="==========\n", stderr="", returncode=0)
    )

    solve_model_path(model_path, data_path=data_path)

    cmd = calls[0]["cmd"]
    assert cmd[-1] == str(data_path.resolve())
    assert cmd[-2] == str(model_path.resolve())


def test_check_passes_compile_flag_on_real_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(monkeypatch, _FakeCompletedProcess(stdout="", stderr="", returncode=0))

    result = check_model_path(model_path)

    cmd = calls[0]["cmd"]
    assert isinstance(result, CheckResult)
    assert result.status == "ok"
    assert "-c" in cmd
    assert "--statistics" not in cmd
    assert cmd[-1] == str(model_path.resolve())
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_find_unsat_core_filters_by_real_basename(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "nurses.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)
    # Traces reference the real basename plus a differently-named included file.
    stdout = (
        "MUS: 1 2\nTraces: nurses.mzn|4|12|4|20|;nurses.mzn|5|12|5|20|;helpers.mzn|10|1|10|5|\n"
    )
    calls = _record_run(monkeypatch, _FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = find_unsat_core_path(model_path)

    assert isinstance(result, UnsatCoreResult)
    assert result.status == "mus_found"
    # Entry-file spans resolve from the real model text; helpers.mzn is excluded.
    assert len(result.core) == 2
    assert "x + y > 5" in result.core[0].source
    assert "x + y < 3" in result.core[1].source
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--solver") + 1] == FINDMUS_SOLVER
    assert "-c" not in cmd
    assert "--statistics" not in cmd
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_find_unsat_core_duplicate_basename_is_known_limitation(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entry model at a/model.mzn; a *different* file b/model.mzn shares the
    # basename. The basename-only filter cannot tell them apart, so a trace
    # span that actually came from b/model.mzn is (mis-)attributed to the entry
    # model's core. This documents the best-effort limitation — raw stdout
    # stays authoritative.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    model_path = tmp_path / "a" / "model.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)
    (tmp_path / "b" / "model.mzn").write_text(_UNSAT_CORE_MODEL)
    stdout = "MUS: 1\nTraces: model.mzn|4|12|4|20|\n"
    _record_run(monkeypatch, _FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = find_unsat_core_path(model_path)

    assert result.status == "mus_found"
    # The collision: the span is attributed to core despite the ambiguity.
    assert len(result.core) == 1
    assert "x + y > 5" in result.core[0].source


def test_compile_error_returns_structured_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text('include "missing.mzn";\nvar 1..5: x;\nsolve satisfy;\n')
    diagnostic = "Error: cannot open included file 'missing.mzn'\n"
    _record_run(monkeypatch, _FakeCompletedProcess(stdout="", stderr=diagnostic, returncode=1))

    result = solve_model_path(model_path)

    # A nonzero-rc compile failure is returned as a structured result, not raised.
    assert result.status == "error"
    assert "cannot open included file" in result.stderr


# --- _validate_model_data_paths --------------------------------------------


def test_validate_rejects_missing_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _validate_model_data_paths(tmp_path / "nope.mzn", None)


def test_validate_rejects_directory_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a file"):
        _validate_model_data_paths(tmp_path, None)


def test_validate_rejects_missing_data(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    with pytest.raises(ValueError, match="does not exist"):
        _validate_model_data_paths(model_path, tmp_path / "missing.dzn")


@pytest.mark.parametrize("body", ["", "   \n\t\n"])
def test_validate_rejects_empty_model(tmp_path: Path, body: str) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(body)
    with pytest.raises(ValueError, match="empty"):
        _validate_model_data_paths(model_path, None)


def test_validate_returns_resolved_absolute_paths(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    data_path = tmp_path / "d.dzn"
    data_path.write_text("n = 1;\n")

    resolved_model, resolved_data = _validate_model_data_paths(model_path, data_path)

    assert resolved_model == model_path.resolve()
    assert resolved_model.is_absolute()
    assert resolved_data == data_path.resolve()
    assert resolved_data is not None and resolved_data.is_absolute()


def test_validate_allows_empty_data(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    data_path = tmp_path / "d.dzn"
    data_path.write_text("")

    _, resolved_data = _validate_model_data_paths(model_path, data_path)

    assert resolved_data == data_path.resolve()


# --- every public function validates before any run ------------------------

_PATH_FUNCS: list[Callable[..., Any]] = [solve_model_path, check_model_path, find_unsat_core_path]


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_missing_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="does not exist"):
        func(tmp_path / "nope.mzn")


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_empty_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    model_path = tmp_path / "empty.mzn"
    model_path.write_text("   \n")
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        func(model_path)


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_non_utf8_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    model_path = tmp_path / "bad.mzn"
    model_path.write_bytes(b"\xff\xfe garbage")
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="UTF-8") as exc_info:
        func(model_path)
    assert str(model_path.resolve()) in str(exc_info.value)


# --- runtime / timeout / exec guards ---------------------------------------


def test_runtime_missing_raises(tmp_path: Path, fake_runtime_dir: Path) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    with pytest.raises(RuntimeMissingError) as exc_info:
        solve_model_path(model_path)
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_non_positive_timeout_raises(
    tmp_path: Path,
    fake_runtime_dir: Path,
    bad_timeout: int,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    with pytest.raises(ValueError, match="positive"):
        solve_model_path(model_path, timeout_ms=bad_timeout)


def test_oserror_wraps_as_execution_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        solve_model_path(model_path)
    assert "install-runtime" in str(exc_info.value)
