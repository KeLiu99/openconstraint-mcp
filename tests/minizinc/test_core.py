from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.core import (
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_INSPECT_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    FINDMUS_SOLVER,
    MiniZincExecutionError,
    _solver_capabilities,
    check_model,
    find_unsat_core,
    inspect_model,
    list_solvers,
    solve_model,
    solver_supports_num_solutions,
)
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas import (
    CheckResult,
    ModelInspectionResult,
    SolveResult,
    SolverList,
)
from tests.minizinc.helpers import (
    STREAM_ERROR,
    STREAM_OPTIMAL,
    STREAM_SATISFY,
    STREAM_SATISFY_ALL,
    STREAM_UNSAT,
    UNSAT_CORE_MODEL,
    UNSAT_CORE_STDOUT,
    FakeCompletedProcess,
    checker_pass,
    checker_violation,
    solution_obj,
    solution_obj_json_only,
    stream,
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
                "stdFlags": ["-a", "-f", "-n", "-p", "-r"],
            },
            {
                "id": "com.google.or-tools.cpsat",
                "name": "OR-Tools CP-SAT",
                "version": "9.10",
                "stdFlags": ["-a", "-i", "-f", "-p", "-r"],
            },
        ]
    )

    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(payload)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    result = list_solvers()
    assert isinstance(result, SolverList)
    assert [solver.id for solver in result.solvers] == [
        "org.gecode.gecode",
        "com.google.or-tools.cpsat",
    ]
    assert result.solvers[0].tags == ["cp", "int"]
    assert result.solvers[1].version == "9.10"
    assert result.solvers[1].tags == []

    # Capabilities flow from each entry's stdFlags. gecode declares -n and is
    # allowlisted, so every control reads True; or-tools declares the standard
    # flags but no -n and is not allowlisted, so supports_num_solutions is False.
    gecode_caps = result.solvers[0].capabilities
    assert gecode_caps.supports_all_solutions is True
    assert gecode_caps.supports_free_search is True
    assert gecode_caps.supports_parallel is True
    assert gecode_caps.supports_random_seed is True
    assert gecode_caps.supports_num_solutions is True

    cpsat_caps = result.solvers[1].capabilities
    assert cpsat_caps.supports_all_solutions is True
    assert cpsat_caps.supports_free_search is True
    assert cpsat_caps.supports_parallel is True
    assert cpsat_caps.supports_random_seed is True
    assert cpsat_caps.supports_num_solutions is False
    assert "Detailed solver capabilities" in result.capability_note


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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    list_solvers()

    assert calls[0]["kwargs"].get("encoding") == "utf-8"


def test_solver_capabilities_gecode_declares_all_controls() -> None:
    caps = _solver_capabilities(
        {"id": "org.gecode.gecode", "stdFlags": ["-a", "-f", "-n", "-p", "-r"]}
    )
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_parallel is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is True
    assert caps.std_flags == ["-a", "-f", "-n", "-p", "-r"]


def test_solver_capabilities_chuffed_lacks_parallel() -> None:
    # chuffed's stdFlags omit -p: supports_parallel must be a real read, not a
    # constant True. The other three derived booleans stay True.
    caps = _solver_capabilities({"id": "org.chuffed.chuffed", "stdFlags": ["-a", "-f", "-n", "-r"]})
    assert caps.supports_parallel is False
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is True


def test_solver_capabilities_cpsat_declares_flags_but_not_num_solutions() -> None:
    # Real cp-sat: declares -a/-f/-p/-r (and -i, which is untracked) but no -n,
    # and is not in the num_solutions allowlist — so supports_num_solutions False.
    caps = _solver_capabilities(
        {"id": "com.google.or-tools.cpsat", "stdFlags": ["-a", "-i", "-f", "-p", "-r"]}
    )
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_parallel is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is False
    assert caps.std_flags == ["-a", "-i", "-f", "-p", "-r"]


def test_solver_capabilities_gist_diverges_from_std_flags() -> None:
    # The load-bearing divergence: gist lists -n in stdFlags but is excluded from
    # the canonical gate, so supports_num_solutions is False even though
    # "-n" stays present in the verbatim std_flags.
    caps = _solver_capabilities(
        {"id": "org.gecode.gist", "stdFlags": ["-a", "-f", "-n", "-p", "-r"]}
    )
    assert caps.supports_num_solutions is False
    assert "-n" in caps.std_flags


def test_solver_capabilities_unknown_without_std_flags_is_default_deny() -> None:
    # No stdFlags key and a non-allowlisted id: empty std_flags zeroes the four
    # derived booleans, and the id is not allowlisted — two independent reasons.
    caps = _solver_capabilities({"id": "com.example.unknown", "name": "Unknown"})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is False


@pytest.mark.parametrize("bad_std_flags", [None, " -a"])
def test_solver_capabilities_malformed_std_flags_non_allowlisted(
    bad_std_flags: object,
) -> None:
    # A present null or a scalar string must degrade to an empty std_flags — never
    # crash on list(None) or split " -a" into ['  ', '-', 'a']. Non-allowlisted id
    # keeps supports_num_solutions False.
    caps = _solver_capabilities({"id": "com.example.unknown", "stdFlags": bad_std_flags})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is False


def test_solver_capabilities_malformed_std_flags_allowlisted_keeps_num_solutions() -> None:
    # The independence invariant: a malformed stdFlags zeroes the four derived
    # booleans, but supports_num_solutions follows the id allowlist and stays True
    # for gecode — never re-coupled to stdFlags.
    caps = _solver_capabilities({"id": "org.gecode.gecode", "stdFlags": None})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is True


def _record_subprocess(
    monkeypatch: pytest.MonkeyPatch, completed: FakeCompletedProcess
) -> list[dict[str, Any]]:
    """Patch subprocess.run to record args/kwargs and return ``completed``.

    Captures the cmd, kwargs, model-file existence, model-file contents, and —
    when a positional ``data.dzn`` or flagged ``checker.mzc.mzn`` is present —
    their contents at the moment ``subprocess.run`` was called. The model/data
    files are positional (model last, or second-last before data); checker files
    live in the flags slot and may also end in ``.mzn``. ``solve_model`` deletes
    the temp dir on return, so post-call reads would race the cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> FakeCompletedProcess:
        cmd = args[0]
        data_path = Path(cmd[-1]) if str(cmd[-1]).endswith(".dzn") else None
        model_path = Path(cmd[-2]) if data_path is not None else Path(cmd[-1])
        checker_path: Path | None = None
        if "--solution-checker" in cmd:
            checker_path = Path(cmd[cmd.index("--solution-checker") + 1])
        data_contents: str | None = None
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        checker_contents: str | None = None
        if checker_path is not None and checker_path.is_file():
            checker_contents = checker_path.read_text()
        calls.append(
            {
                "args": args,
                "kwargs": kwargs,
                "model_path": str(model_path),
                "model_path_existed": model_path.is_file(),
                "model_contents": (model_path.read_text() if model_path.is_file() else None),
                "data_contents": data_contents,
                "checker_contents": checker_contents,
            }
        )
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)
    return calls


def test_solve_model_happy_path_returns_satisfied(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single `satisfy` solve emits a solution but no status object, so it is
    # classified `satisfied` from the clean exit + solution — not `optimal` (the
    # classic-`==========` misread this whole change exists to kill).
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
    stdout = stream(solution_obj_json_only({"x": 3}))
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_OPTIMAL, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model(model)

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
    assert "--solution-checker" not in cmd
    assert result.checker is None

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


_CHECKER_SRC = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)


def test_solve_model_with_checker_adds_solution_checker_flag(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0)
    )

    result = solve_model(
        "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;", checker=_CHECKER_SRC
    )

    cmd = calls[0]["args"][0]
    checker_arg = cmd[cmd.index("--solution-checker") + 1]
    assert Path(checker_arg).name == "checker.mzc.mzn"
    assert calls[0]["checker_contents"] == _CHECKER_SRC
    assert cmd.index("--solution-checker") < cmd.index(calls[0]["model_path"])
    assert result.checker is not None
    assert result.checker.status == "completed"


def test_solve_model_with_checker_incorrect_text_is_not_a_violation(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("INCORRECT\n"),
        solution_obj("x=3 y=1\n", {"x": 3, "y": 1}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "completed"
    assert result.checker.checks[0].violation is False
    assert result.checker.checks[0].output == "INCORRECT\n"


def test_solve_model_with_checker_violation_keeps_rejected_solution(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_violation(),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "violation"
    assert result.checker.checks[0].violation is True
    assert result.solutions == [{"x": 1, "y": 2}]


def test_solve_model_with_checker_missing_verdict_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_stream_error_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream({"type": "status", "status": "ERROR"})
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("solve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "error"
    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_top_level_error_and_nested_unknown_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        {
            "type": "error",
            "what": "result of evaluation is undefined",
            "message": "array access out of bounds",
        },
        {"type": "checker", "messages": [{"type": "status", "status": "UNKNOWN"}]},
        solution_obj_json_only({"x": 2}),
    )
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nconstraint x = 2;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "error"
    assert result.return_code == 0
    assert result.checker is not None
    assert result.checker.status == "error"
    assert len(result.checker.checks) == 1
    assert result.checker.checks[0].violation is False
    assert result.solutions == [{"x": 2}]


def test_solve_model_with_checker_nonzero_rc_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="boom", returncode=1)
    )

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_no_solution_status(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream({"type": "status", "status": "UNSATISFIABLE"})
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("constraint false;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "unsatisfiable"
    assert result.checker is not None
    assert result.checker.status == "no_solution"


def test_solve_model_with_checker_timeout_status(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0), output="")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    result = solve_model("solve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "timeout"
    assert result.timed_out is True


def test_solve_model_with_checker_transcript_is_raw_stdout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_subprocess(monkeypatch, FakeCompletedProcess(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.transcript == stdout
    assert "checker" in result.checker.transcript
    assert "checker" not in result.stdout


# --- Phase 2: solver/search-control flags ----------------------------------


def _solve_cmd_with_flags(monkeypatch: pytest.MonkeyPatch, **flags: Any) -> list[str]:
    """Solve a trivial model with the given flags; return the argv it built."""
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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


def test_solve_model_checker_composes_with_all_solutions(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(monkeypatch, checker=_CHECKER_SRC, all_solutions=True)
    assert "-a" in cmd
    assert "--solution-checker" in cmd


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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="parallel"):
        solve_model("solve satisfy;", parallel=bad)


# --- num_solutions (solver-gated) ------------------------------------------


@pytest.mark.parametrize("solver", ["org.chuffed.chuffed", "org.gecode.gecode"])
def test_solve_model_num_solutions_adds_valued_n_flag_for_supported_solver(
    solver: str, fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A canonical -n-supporting solver gets `-n N` appended alongside the transport.
    cmd = _solve_cmd_with_flags(monkeypatch, num_solutions=2, solver=solver)
    assert cmd[cmd.index("-n") + 1] == "2"


def test_solve_model_num_solutions_rejects_short_alias(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The allowlist is canonical-ids-only: a short alias like "gecode" degrades to
    # the actionable error rather than a broken -n command (Decision 2).
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for an unsupported solver")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", num_solutions=2, solver="gecode")
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


def test_solve_model_num_solutions_rejected_for_default_solver(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default solver (cp-sat) does not support -n; the gate raises before any
    # subprocess so the doomed command is never built.
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", num_solutions=2)
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


def test_solve_model_num_solutions_rejected_before_checker_run_for_default_solver(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", checker=_CHECKER_SRC, num_solutions=2)
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.parametrize("bad", [0, -1])
def test_solve_model_rejects_non_positive_num_solutions(
    bad: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions"):
        solve_model("solve satisfy;", num_solutions=bad, solver="org.chuffed.chuffed")


def test_solve_model_default_omits_num_solutions_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With num_solutions unset, no -n token appears (Phase-1 argv preserved).
    assert "-n" not in _solve_cmd_with_flags(monkeypatch)


@pytest.mark.parametrize(
    ("solver", "expected"),
    [
        ("org.gecode.gecode", True),
        ("org.chuffed.chuffed", True),
        ("cp-sat", False),
        ("gecode", False),
        ("org.gecode.gist", False),
    ],
)
def test_solver_supports_num_solutions_truth_table(solver: str, expected: bool) -> None:
    assert solver_supports_num_solutions(solver) is expected


def test_solve_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
        FakeCompletedProcess(stdout="", stderr=diagnostic, returncode=1),
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        solve_model(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_solve_model_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

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
        FakeCompletedProcess(
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
        FakeCompletedProcess(stdout=STREAM_UNSAT, stderr="", returncode=0),
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY_ALL, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_ERROR, stderr="", returncode=1)
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
        stream(solution_obj("x=3\n", {"x": 3})) + '{"type": "stat'  # truncated tail
    ).encode("utf-8")

    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=partial,
            stderr=b"",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        solve_model("solve satisfy;")
    assert "install-runtime" in str(exc_info.value)


def test_solve_model_decodes_output_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("% café λ\nsolve satisfy;")

    assert captured["encoding"] == "utf-8"


def test_check_model_happy_path_returns_ok(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        monkeypatch, FakeCompletedProcess(stdout="", stderr="", returncode=0)
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
        FakeCompletedProcess(
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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        check_model(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_check_model_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        check_model("solve satisfy;")
    assert "install-runtime" in str(exc_info.value)


def test_find_unsat_core_mus_found_preserves_raw_output(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "mus_found"
    assert len(result.core) == 2
    assert result.stdout == UNSAT_CORE_STDOUT
    assert "minimal unsatisfiable subset" in result.message


def test_find_unsat_core_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

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
    assert calls[0]["model_contents"] == UNSAT_CORE_MODEL
    assert kwargs["cwd"] == str(model_path.parent)


def test_find_unsat_core_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL, data="lo = 5;")

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
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

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
        FakeCompletedProcess(stdout="=====UNKNOWN=====\n", stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "no_core"
    assert result.core == []


def test_find_unsat_core_error_clears_structured_core_and_preserves_stderr(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "Error: cannot load solver org.minizinc.findmus\n"
    _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout="", stderr=stderr, returncode=1),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    result = find_unsat_core(UNSAT_CORE_MODEL)

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

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        find_unsat_core(bad_model)


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_find_unsat_core_rejects_non_positive_timeout(
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        find_unsat_core(UNSAT_CORE_MODEL, timeout_ms=bad_timeout)


def test_find_unsat_core_raises_when_runtime_missing(fake_runtime_dir: Path) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        find_unsat_core(UNSAT_CORE_MODEL)
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_find_unsat_core_wraps_oserror_as_execution_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        find_unsat_core(UNSAT_CORE_MODEL)
    assert "install-runtime" in str(exc_info.value)


def test_find_unsat_core_uses_default_timeout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)


# --- inspect_model (--model-interface-only) --------------------------------

_INSPECT_KNAPSACK_MODEL = (
    "int: n;\narray[1..n] of int: weight;\narray[1..n] of var bool: take;\n"
    "solve maximize sum(i in 1..n)(weight[i] * take[i]);\n"
)
# A single-line interface object as the managed binary emits it (captured shape).
_INSPECT_KNAPSACK_STDOUT = (
    '{"type": "interface", "input": {"n": {"type": "int"}, "weight": '
    '{"type": "int", "dim": 1}}, "output": {"take": {"type": "bool", "dim": 1}}, '
    '"method": "max", "has_output_item": false, "included_files": [], "globals": []}'
)


def test_inspect_model_happy_path_returns_ok(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=_INSPECT_KNAPSACK_STDOUT, stderr="", returncode=0),
    )

    result = inspect_model(_INSPECT_KNAPSACK_MODEL)

    assert isinstance(result, ModelInspectionResult)
    assert result.status == "ok"
    assert result.solver == "cp-sat"
    assert result.interface is not None
    assert result.interface.method == "max"
    assert set(result.interface.required_parameters) == {"n", "weight"}
    assert result.interface.required_parameters["weight"].dim == 1
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_inspect_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=_INSPECT_KNAPSACK_STDOUT, stderr="", returncode=0),
    )

    inspect_model(_INSPECT_KNAPSACK_MODEL)

    cmd = calls[0]["args"][0]
    assert "--model-interface-only" in cmd
    # Inspection is read-only type analysis: never the solve transport, the
    # statistics flag, or any solve-only search control.
    assert "--json-stream" not in cmd
    assert "--statistics" not in cmd
    assert not ({"-a", "-f", "-p", "-r", "-n"} & set(cmd))
    # The interface flag precedes the model file (extra_args go before the model).
    assert cmd.index("--model-interface-only") < cmd.index(
        next(arg for arg in cmd if str(arg).endswith(".mzn"))
    )


def test_inspect_model_returns_structured_error_for_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        ),
    )

    result = inspect_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert result.interface is None
    assert "type error" in result.stderr


def test_inspect_model_degrades_to_error_on_unparseable_stdout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # rc 0 but stdout is not a parseable interface object: rather than mis-report a
    # partial interface, the result degrades to status="error" with interface None.
    _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout="not an interface line\n", stderr="", returncode=0),
    )

    result = inspect_model("var 1..5: x;\nsolve satisfy;")

    assert result.status == "error"
    assert result.interface is None
    # The raw stdout is preserved so the failure is diagnosable.
    assert "not an interface line" in result.stdout


def test_inspect_model_returns_timeout_when_run_times_out(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hard timeout during interface extraction classifies as status="timeout" with
    # no interface, completing the status matrix alongside ok / error-compile /
    # error-unparseable. Driven by a raised TimeoutExpired, the same idiom check uses.
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout", 35.0),
            output=b"partial",
            stderr=b"",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    result = inspect_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.interface is None
    assert result.stdout == "partial"
    assert result.stderr == ""


def test_inspect_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_interface = (
        '{"type": "interface", "input": {}, "output": {"x": {"type": "int"}}, '
        '"method": "sat", "has_output_item": false}'
    )
    calls = _record_subprocess(
        monkeypatch,
        FakeCompletedProcess(stdout=empty_interface, stderr="", returncode=0),
    )

    result = inspect_model("int: n;\nvar 1..n: x;\nsolve satisfy;", data="n = 3;")

    cmd = calls[0]["args"][0]
    assert "--model-interface-only" in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "n = 3;"
    # Data supplied -> the interface reports nothing still required (completeness).
    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters == {}


@pytest.mark.parametrize("bad_model", ["", "\n\n  \t\n"])
def test_inspect_model_rejects_empty_or_whitespace_model(
    bad_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        inspect_model(bad_model)


def test_inspect_model_rejects_non_positive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        inspect_model("solve satisfy;", timeout_ms=0)


def test_inspect_model_raises_when_runtime_missing(fake_runtime_dir: Path) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        inspect_model("solve satisfy;")
    assert "install-runtime" in str(exc_info.value)


def test_inspect_default_timeout_aliases_check_budget() -> None:
    # Decision 7: inspection is a preflight like check, so it shares the check
    # budget rather than an independent literal that could drift.
    assert DEFAULT_INSPECT_TIMEOUT_MS == DEFAULT_CHECK_TIMEOUT_MS
