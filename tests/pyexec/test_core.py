"""Unit tests for pyexec/core.py — all subprocess calls mocked."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openconstraint_mcp.pyexec.core import (
    effective_checker_timeout_ms,
    run_cpsat_python,
    run_cpsat_python_file,
    run_cpsat_python_file_checked,
    seed_config_env,
    validate_checker_args,
    validate_cpsat_random_seed,
)
from openconstraint_mcp.pyexec.core import (
    normalize_objective as _normalize_objective,
)
from openconstraint_mcp.pyexec.diagnostics import (
    checker_report_diagnostic,
    cpsat_result_diagnostic,
)
from openconstraint_mcp.pyexec.eligibility import diagnostic_incumbent_eligibility
from openconstraint_mcp.schemas.cpsat import CpsatCheckerReport, CpsatPythonResult
from openconstraint_mcp.shared.childrun import MAX_OUTPUT_BYTES, ChildSpawnError

_VALID_SOLUTION = {"x": 3, "y": 7}
_VALID_STDOUT = json.dumps({"status": "optimal", "objective": 10, "solution": _VALID_SOLUTION})


def _make_fake_proc(
    *,
    returncode: int = 0,
    stdout_content: str = _VALID_STDOUT,
    stderr_content: str = "",
    timeout: bool = False,
    output_size: int | None = None,
) -> MagicMock:
    """Return a fake Popen handle."""
    fake = MagicMock()
    fake.pid = 1234
    fake.returncode = None if timeout or output_size else returncode

    def _poll() -> int | None:
        return fake.returncode

    fake.poll = _poll

    if timeout:
        fake.wait.return_value = returncode
        fake.returncode = returncode
    elif output_size is not None:
        fake.returncode = returncode
        fake.wait.return_value = returncode
    else:
        fake.wait.return_value = returncode
        fake.returncode = returncode

    return fake


def _run_with_mocked_proc(
    source: str = "print('hi')",
    *,
    stdout_content: str = _VALID_STDOUT,
    stderr_content: str = "",
    returncode: int = 0,
    timeout: bool = False,
    large_output: bool = False,
    timeout_ms: int = 5000,
    tracker: Any = None,
) -> CpsatPythonResult:
    """Run run_cpsat_python with all subprocess/proc calls patched."""

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None  # live

        # Simulate file writes
        stdout_file = kwargs.get("stdout")
        stderr_file = kwargs.get("stderr")

        actual_stdout = "x" * (MAX_OUTPUT_BYTES + 1) if large_output else stdout_content
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(actual_stdout)
            stdout_file.flush()
        if stderr_file and hasattr(stderr_file, "write"):
            stderr_file.write(stderr_content)
            stderr_file.flush()

        # Make poll() return None initially (live process)
        _poll_count = [0]

        def _poll() -> int | None:
            _poll_count[0] += 1
            if timeout and _poll_count[0] < 2:
                return None
            if large_output and _poll_count[0] < 2:
                return None
            fake.returncode = returncode
            return returncode

        if timeout:
            # Process never finishes on its own
            def _poll_timeout() -> int | None:
                return None

            fake.poll = _poll_timeout

            # Real Popen.wait() reaps the killed child and sets .returncode (e.g.
            # -15 for SIGTERM). Mirror that so the executor's null-on-timeout
            # override is actually exercised, not masked by a None left on the mock.
            def _wait_sets_returncode(*_a: Any, **_k: Any) -> int:
                fake.returncode = returncode
                return returncode

            fake.wait.side_effect = _wait_sets_returncode
        else:
            fake.poll = _poll
            fake.wait.return_value = returncode

        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree") as mock_kill,
    ):
        result = run_cpsat_python(source, timeout_ms=timeout_ms, tracker=tracker)
    result._mock_kill = mock_kill  # type: ignore[attr-defined]
    return result


class _SpyTracker:
    """Records register/unregister calls so wiring can be asserted without a kill."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register(self, proc: Any) -> None:
        self.events.append(("register", proc))

    def unregister(self, proc: Any) -> None:
        self.events.append(("unregister", proc))


# --- shared validation helpers ----------------------------------------------


def test_validate_checker_args_accepts_valid_checker_timeout_pair() -> None:
    validate_checker_args(checker="print('ok')", checker_timeout_ms=100)


def test_validate_checker_args_rejects_timeout_without_checker() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms supplied without checker"):
        validate_checker_args(checker=None, checker_timeout_ms=100)


def test_validate_checker_args_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms must be positive"):
        validate_checker_args(checker="print('ok')", checker_timeout_ms=0)


def test_validate_checker_args_rejects_blank_checker() -> None:
    with pytest.raises(ValueError, match="checker must be non-empty"):
        validate_checker_args(checker="   ", checker_timeout_ms=None)


def test_effective_checker_timeout_uses_explicit_value_or_default() -> None:
    assert effective_checker_timeout_ms(checker_timeout_ms=250, default_timeout_ms=1000) == 250
    assert effective_checker_timeout_ms(checker_timeout_ms=None, default_timeout_ms=1000) == 1000


@pytest.mark.parametrize("seed", [-2_147_483_648, -1, 0, 2_147_483_647])
def test_validate_cpsat_random_seed_accepts_signed_int32(seed: int) -> None:
    assert validate_cpsat_random_seed(seed) == seed


@pytest.mark.parametrize("seed", [True, False, 1.5, "7"])
def test_validate_cpsat_random_seed_rejects_non_integer_values(seed: object) -> None:
    with pytest.raises(ValueError, match="non-bool integer"):
        validate_cpsat_random_seed(seed)


@pytest.mark.parametrize("seed", [-2_147_483_649, 2_147_483_648])
def test_validate_cpsat_random_seed_rejects_out_of_signed_int32_range(seed: int) -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        validate_cpsat_random_seed(seed)


# (a) valid JSON → parsed status/solution
def test_run_cpsat_python_parses_valid_solution() -> None:
    result = _run_with_mocked_proc(stdout_content=_VALID_STDOUT)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10
    assert result.timed_out is False
    assert result.truncated is False


# (b) non-zero exit → status="error", stderr surfaced
def test_run_cpsat_python_nonzero_exit_yields_error() -> None:
    result = _run_with_mocked_proc(
        stdout_content="bad output",
        stderr_content="something failed",
        returncode=1,
    )

    assert result.status == "error"
    assert "failed" in result.stderr


# (c) timeout → timed_out, status="timeout", tree-kill invoked
def test_run_cpsat_python_timeout_kills_tree_and_sets_status() -> None:
    result = _run_with_mocked_proc(timeout=True, timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result._mock_kill.called  # type: ignore[attr-defined]


# (c1) a non-positive timeout is rejected before any child is spawned, matching
# the MiniZinc path's validate_model_and_timeout.
@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_run_cpsat_python_non_positive_timeout_raises(timeout_ms: int) -> None:
    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="timeout_ms must be positive"):
            run_cpsat_python("print('x')", timeout_ms=timeout_ms)
    fake_popen.assert_not_called()


# (c2) the child interpreter is launched unbuffered (-u) so prints reach the
# capture files in real time and survive a timeout kill.
def test_run_cpsat_python_launches_child_unbuffered() -> None:
    captured: dict[str, list[str]] = {}

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["cmd"] = cmd
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(_VALID_STDOUT)
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python("print('hi')", timeout_ms=5000)

    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1] == "-u"  # unbuffered, before the script path


# (c3) on timeout, an intermediate JSON block (best-so-far from a callback) is
# recovered into solution/objective; status stays the executor-owned "timeout".
def test_run_cpsat_python_timeout_recovers_partial_solution() -> None:
    partial = json.dumps({"status": "feasible", "objective": 3, "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.solution == {"x": 1}
    assert result.objective == 3


# (c4) timeout with no parseable JSON keeps solution/objective None.
def test_run_cpsat_python_timeout_without_partial_has_no_solution() -> None:
    result = _run_with_mocked_proc(timeout=True, stdout_content="searching...\n", timeout_ms=50)

    assert result.status == "timeout"
    assert result.solution is None
    assert result.objective is None


# (c5) on timeout the killed child's exit code (SIGTERM -> -15) is reported as null,
# matching the documented contract (README: return_code "null on timeout") so a
# timeout is not misread as a child error. The mock sets returncode=-15 on wait, so
# this fails if the executor forwards it instead of overriding to None.
def test_run_cpsat_python_timeout_return_code_is_none() -> None:
    result = _run_with_mocked_proc(timeout=True, returncode=-15, timeout_ms=50)

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.return_code is None


# (d) unparseable stdout → status="error"
def test_run_cpsat_python_unparseable_stdout_yields_error() -> None:
    result = _run_with_mocked_proc(stdout_content="not json at all")

    assert result.status == "error"
    assert result.solution is None


# (f) off-vocabulary status → normalized to "error"
def test_run_cpsat_python_off_vocabulary_status_normalized_to_error() -> None:
    bad_status = json.dumps({"status": "MODEL_INVALID", "objective": None, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=bad_status)

    assert result.status == "error"
    # Must not raise — CpsatPythonResult must be constructable
    assert isinstance(result, CpsatPythonResult)


# (g) a script may not self-report "timeout" — only the executor sets it
def test_run_cpsat_python_script_reported_timeout_normalized_to_error() -> None:
    forged = json.dumps({"status": "timeout", "objective": None, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=forged)

    assert result.status == "error"
    assert result.timed_out is False


# (h) a non-numeric objective is a CONTRACT ERROR, not a silent null: the field
# has always been documented as number-or-null, and permissive normalization hid
# a broken emit block behind a plausible-looking result.
def test_run_cpsat_python_non_numeric_objective_yields_contract_error() -> None:
    payload = json.dumps({"status": "optimal", "objective": "lots", "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "error"
    assert result.solution is None
    assert result.diagnostic is not None
    assert result.diagnostic.details == {
        "field": "objective",
        "reason": "must be a finite number or null",
        "return_code": 0,
    }


# (h3) best_objective_bound is parsed even for status="unknown", where no
# incumbent/objective was found — this is the diagnostic signal the field exists for.
def test_run_cpsat_python_parses_best_objective_bound_for_unknown_status() -> None:
    payload = json.dumps(
        {"status": "unknown", "objective": None, "solution": {}, "best_objective_bound": 5}
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "unknown"
    assert result.objective is None
    assert result.best_objective_bound == 5


# (h4) an old script that never emits best_objective_bound must still parse cleanly.
def test_run_cpsat_python_missing_best_objective_bound_is_none() -> None:
    result = _run_with_mocked_proc(stdout_content=_VALID_STDOUT)

    assert result.status == "optimal"
    assert result.best_objective_bound is None


# (h5) invalid best_objective_bound values (bool, non-numeric) are normalized to None,
# matching normalize_objective's rules exactly.
@pytest.mark.parametrize("raw", [True, "lots"])
def test_run_cpsat_python_invalid_best_objective_bound_becomes_none(raw: object) -> None:
    payload = json.dumps(
        {"status": "unknown", "objective": None, "solution": {}, "best_objective_bound": raw}
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.best_objective_bound is None


# (h6) on timeout, a recovered intermediate JSON block's best_objective_bound is
# carried through exactly like solution/objective.
def test_run_cpsat_python_timeout_recovers_partial_best_objective_bound() -> None:
    partial = json.dumps(
        {"status": "feasible", "objective": 3, "solution": {"x": 1}, "best_objective_bound": 1}
    )
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, timeout_ms=50)

    assert result.status == "timeout"
    assert result.best_objective_bound == 1


# --- required stdout envelope ----------------------------------------------
#
# `status`, `objective`, and `solution` are REQUIRED and type-checked on a clean
# exit. A violation is status="error" with no incumbent and a child_process_error
# diagnostic naming the offending field — the only client-visible transport for
# it (there is deliberately no public result field).


def _envelope_error_field(payload: dict) -> str:
    """Run a payload through the executor and return the diagnosed field name."""
    result = _run_with_mocked_proc(stdout_content=json.dumps(payload))
    assert result.status == "error"
    assert result.diagnostic is not None
    details = result.diagnostic.details
    assert details is not None
    return str(details["field"])


def test_run_cpsat_python_missing_status_is_a_contract_error() -> None:
    assert _envelope_error_field({"objective": 1, "solution": {"x": 1}}) == "status"


def test_run_cpsat_python_missing_objective_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": "optimal", "solution": {"x": 1}}) == "objective"


def test_run_cpsat_python_missing_solution_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": "optimal", "objective": 1}) == "solution"


def test_run_cpsat_python_non_string_status_is_a_contract_error() -> None:
    assert _envelope_error_field({"status": 3, "objective": 1, "solution": {"x": 1}}) == "status"


def test_run_cpsat_python_off_vocabulary_status_names_the_status_field() -> None:
    # The status still normalizes to "error" (see the (f) case above); what is new
    # is that the diagnostic says WHICH field was wrong.
    payload = {"status": "MODEL_INVALID", "objective": None, "solution": {}}
    assert _envelope_error_field(payload) == "status"


def test_run_cpsat_python_off_vocabulary_status_drops_the_solution() -> None:
    # A violation yields no incumbent, so an off-vocabulary status no longer
    # carries its solution through the way the pre-envelope normalization did.
    payload = json.dumps({"status": "MODEL_INVALID", "objective": 1, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.solution is None


def test_run_cpsat_python_non_object_solution_is_a_contract_error() -> None:
    payload = {"status": "optimal", "objective": 1, "solution": [{"x": 1}]}
    assert _envelope_error_field(payload) == "solution"


def test_run_cpsat_python_null_solution_is_a_contract_error() -> None:
    # `null` is the shape seen in the wild: a run with no incumbent must still
    # emit `{}`, or a legitimate infeasible/unknown result becomes an error.
    payload = {"status": "infeasible", "objective": None, "solution": None}
    assert _envelope_error_field(payload) == "solution"


def test_run_cpsat_python_contract_error_diagnostic_details_are_field_reason_return_code() -> None:
    result = _run_with_mocked_proc(stdout_content=json.dumps({"status": "optimal", "objective": 1}))

    assert result.diagnostic is not None
    assert result.diagnostic.details == {
        "field": "solution",
        "reason": "required key is missing",
        "return_code": 0,
    }


def test_run_cpsat_python_contract_error_preserves_raw_streams() -> None:
    payload = json.dumps({"status": "optimal", "objective": 1})
    result = _run_with_mocked_proc(stdout_content=payload, stderr_content="a warning")

    assert result.stdout == payload
    assert result.stderr == "a warning"


def test_run_cpsat_python_null_objective_is_valid_for_a_feasibility_model() -> None:
    payload = json.dumps({"status": "feasible", "objective": None, "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "feasible"
    assert result.objective is None


def test_run_cpsat_python_extra_envelope_keys_are_accepted() -> None:
    payload = json.dumps(
        {
            "status": "optimal",
            "objective": 10,
            "solution": _VALID_SOLUTION,
            "stats": {"conflicts": 3},
            "result_file": "/tmp/out.json",
        }
    )
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.diagnostic is None


def test_run_cpsat_python_empty_solution_keeps_the_specific_diagnostic() -> None:
    # `{}` is a WELL-TYPED solution: emptiness is an acceptance rule, so this must
    # stay the more specific "reported a status but emitted no solution" branch,
    # not be reclassified as a malformed envelope.
    payload = json.dumps({"status": "optimal", "objective": 10, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.diagnostic is not None
    assert result.diagnostic.details == {"status": "optimal"}


def test_run_cpsat_python_empty_solution_fails_the_incumbent_eligibility_gate() -> None:
    payload = json.dumps({"status": "optimal", "objective": 10, "solution": {}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert diagnostic_incumbent_eligibility(result) == (False, "solution is missing or empty")


def test_run_cpsat_python_malformed_timeout_partial_is_not_recovered() -> None:
    partial = json.dumps({"status": "feasible", "solution": {"x": 1}})  # no `objective`
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, timeout_ms=50)

    assert result.solution is None


def test_run_cpsat_python_off_vocabulary_timeout_partial_is_not_recovered() -> None:
    # An intermediate block is where a script is most likely to invent a status;
    # the envelope gate drops it rather than recovering an unclassifiable partial.
    partial = json.dumps({"status": "in_progress", "objective": 3, "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, timeout_ms=50)

    assert result.solution is None


def test_run_cpsat_python_malformed_timeout_partial_keeps_the_timeout_diagnostic() -> None:
    # Timeout is executor-owned and its diagnostic keeps precedence: a malformed
    # partial must never turn the run into a protocol error.
    partial = json.dumps({"status": "feasible", "solution": {"x": 1}})
    result = _run_with_mocked_proc(timeout=True, stdout_content=partial, timeout_ms=50)

    assert result.status == "timeout"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "timeout_no_incumbent"


# (h2) trailing output after the JSON block must not defeat parsing, and a nested
# object inside the payload must not be mistaken for the result.
def test_run_cpsat_python_parses_json_with_trailing_output() -> None:
    noisy = _VALID_STDOUT + "\n[INFO] solver shutdown complete\n"
    result = _run_with_mocked_proc(stdout_content=noisy)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10


# (i) a fast-exiting script that still overran the cap is flagged truncated
def test_run_cpsat_python_fast_exit_large_output_is_flagged_truncated() -> None:
    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0  # already exited before the first poll
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write("x" * (MAX_OUTPUT_BYTES + 1))
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = run_cpsat_python("print('hi')", timeout_ms=5000)

    assert result.truncated is True
    assert result.status == "error"


# --- run_cpsat_python_file: path-based variant -----------------------------


def _run_file_with_mocked_proc(
    script_path: Path,
    *,
    stdout_content: str = _VALID_STDOUT,
    returncode: int = 0,
    timeout_ms: int = 5000,
    args: list[str] | None = None,
    tracker: Any = None,
    env: dict[str, str] | None = None,
) -> tuple[CpsatPythonResult, dict[str, Any]]:
    """Run run_cpsat_python_file with popen patched; capture the popen call."""
    captured: dict[str, Any] = {}

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["cmd"] = cmd
        captured.update(kwargs)
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = returncode
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(stdout_content)
            stdout_file.flush()
        fake.poll = lambda: returncode
        fake.wait.return_value = returncode
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = run_cpsat_python_file(
            script_path, timeout_ms=timeout_ms, args=args, tracker=tracker, env=env
        )
    return result, captured


# (k) a valid script file delegates to the same execution/parse path as inline.
def test_run_cpsat_python_file_parses_valid_solution(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")

    result, _ = _run_file_with_mocked_proc(script)

    assert result.status == "optimal"
    assert result.solution == _VALID_SOLUTION
    assert result.objective == 10


# (k1) the key value-add: the script runs in its OWN directory (cwd=parent), so a
# relative open()/import resolves — unlike inline, which runs in a throwaway tempdir.
def test_run_cpsat_python_file_runs_in_script_directory(tmp_path: Path) -> None:
    script = tmp_path / "sub" / "model.py"
    script.parent.mkdir()
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script)

    assert captured["cwd"] == str(script.parent.resolve())


# (k2) argv runs the real file path unbuffered (-u), not a copy.
def test_run_cpsat_python_file_argv_targets_file_unbuffered(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script)

    assert captured["cmd"] == [sys.executable, "-u", str(script.resolve())]


# (k2a) `args` trail the script path, so the child reads them as sys.argv[1:].
def test_run_cpsat_python_file_appends_args_after_script_path(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script, args=["data_ft10.json"])

    assert captured["cmd"] == [sys.executable, "-u", str(script.resolve()), "data_ft10.json"]


# (k3) tracker is registered then unregistered on the file path too.
def test_run_cpsat_python_file_registers_then_unregisters_child(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")
    tracker = _SpyTracker()

    _run_file_with_mocked_proc(script, tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]


# (k4) a missing path is rejected before any child is spawned.
def test_run_cpsat_python_file_missing_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="does not exist"):
            run_cpsat_python_file(missing)
    fake_popen.assert_not_called()


def test_run_cpsat_python_file_nul_arg_raises_before_spawn(tmp_path: Path) -> None:
    """A NUL is caught in validation, not by Popen's own `embedded null byte`."""
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match=r"args\[0\] contains a NUL character"):
            run_cpsat_python_file(script, args=["\0"])
    fake_popen.assert_not_called()


# --- on_start hook -----------------------------------------------------------


def test_run_cpsat_python_no_on_start_default_is_none() -> None:
    """Omitting on_start (default None) behaves identically to the old API."""
    result = _run_with_mocked_proc()

    assert result.status == "optimal"


def test_run_cpsat_python_file_on_start_called_once(tmp_path: Path) -> None:
    """on_start works on the file-path entry point too."""
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")
    received: list[Any] = []

    _, _ = _run_file_with_mocked_proc(script)  # baseline: no on_start

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 7777
        fake.returncode = 0
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(_VALID_STDOUT)
            stdout_file.flush()
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python_file(script, timeout_ms=5000, on_start=lambda p: received.append(p))

    assert len(received) == 1
    assert received[0].pid == 7777


# (k5) a directory is not a runnable script.
def test_run_cpsat_python_file_directory_raises(tmp_path: Path) -> None:
    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not a file"):
            run_cpsat_python_file(tmp_path)
    fake_popen.assert_not_called()


# (k6) an empty/whitespace-only script is rejected with a clear error.
def test_run_cpsat_python_file_empty_file_raises(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text("   \n", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="is empty"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k7) a non-UTF-8 file surfaces a clear ValueError, not an opaque decode traceback.
def test_run_cpsat_python_file_non_utf8_raises(tmp_path: Path) -> None:
    script = tmp_path / "latin1.py"
    script.write_bytes(b"print('caf\xe9')")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not valid UTF-8"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k8) a non-positive timeout is rejected before any child is spawned.
@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_run_cpsat_python_file_non_positive_timeout_raises(tmp_path: Path, timeout_ms: int) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="timeout_ms must be positive"):
            run_cpsat_python_file(script, timeout_ms=timeout_ms)
    fake_popen.assert_not_called()


# --- _normalize_objective tests -------------------------------------------


def test_normalize_objective_accepts_int() -> None:
    assert _normalize_objective(42) == 42


def test_normalize_objective_accepts_float() -> None:
    assert _normalize_objective(3.14) == 3.14


def test_normalize_objective_accepts_zero() -> None:
    assert _normalize_objective(0) == 0


def test_normalize_objective_rejects_bool_true() -> None:
    assert _normalize_objective(True) is None


def test_normalize_objective_rejects_bool_false() -> None:
    assert _normalize_objective(False) is None


def test_normalize_objective_rejects_nan() -> None:
    assert _normalize_objective(math.nan) is None


def test_normalize_objective_rejects_positive_inf() -> None:
    assert _normalize_objective(math.inf) is None


def test_normalize_objective_rejects_negative_inf() -> None:
    assert _normalize_objective(-math.inf) is None


def test_normalize_objective_rejects_string() -> None:
    assert _normalize_objective("10") is None


def test_normalize_objective_rejects_none() -> None:
    assert _normalize_objective(None) is None


def test_normalize_objective_accepts_huge_int_without_overflow() -> None:
    # A CP-SAT objective too large to convert to a float must not crash
    # (math.isfinite would raise OverflowError); the exact int is preserved.
    big = 10**400
    assert _normalize_objective(big) == big


# --- internal env overlay ----------------------------------------------------


def _capture_popen_env(source: str, *, env: dict[str, str | None] | None) -> dict[str, str] | None:
    """Run run_cpsat_python with a fake Popen and return the env kwarg it received."""
    captured: dict[str, Any] = {}

    def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured["env"] = kwargs.get("env")
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        fake.poll = lambda: 0
        fake.wait.return_value = 0
        return fake

    with (
        patch("openconstraint_mcp.shared.childrun.popen_process_group", side_effect=_fake_popen),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        run_cpsat_python(source, timeout_ms=1000, env=env)
    return captured["env"]


def test_seed_config_env_always_returns_both_keys() -> None:
    # Both protocol keys are always present, set to the requested value or
    # explicit None — never omitted — so a caller can't accidentally build an
    # overlay that leaves an unrequested key to whatever the parent process
    # happens to have inherited.
    assert seed_config_env(seed=None, config_path=None) == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert seed_config_env(seed=7, config_path=None) == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_env_overlay_none_value_clears_stale_parent_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: a server process launched from a shell that already
    # exports OPENCONSTRAINT_MCP_CPSAT_CONFIG (e.g. leftover from manual
    # testing) must not leak that stale value into a child whose caller
    # explicitly requested no config. Before the fix, execute_child's env
    # overlay only ever added keys on top of os.environ, so an unrequested key
    # silently passed through from the parent's environment; seed_config_env
    # now emits an explicit None for it, and execute_child must delete it.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", "/stale/leftover-config.json")

    env = _capture_popen_env(
        "print('x')",
        env=seed_config_env(seed=None, config_path=None),
    )

    assert env is not None
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" not in env
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" not in env
    # Unrelated inherited variables are untouched.
    assert "PATH" in env


def test_env_overlay_none_value_clears_stale_var_even_with_other_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same leak, but for the "seed requested, config not" combination: only
    # setting the seed key in the overlay must not let a stale config var
    # ride along from the parent's environment.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", "/stale/leftover-config.json")

    env = _capture_popen_env(
        "print('x')",
        env=seed_config_env(seed=7, config_path=None),
    )

    assert env is not None
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" not in env


def test_run_cpsat_python_file_forwards_env_overlay(tmp_path: Path) -> None:
    # run_cpsat_python_file mirrors run_cpsat_python's env overlay: same execute_child,
    # so the same OPENCONSTRAINT_MCP_CPSAT_SEED-style overlay must reach the child here too.
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")

    _, captured = _run_file_with_mocked_proc(script, env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"})

    assert captured["env"]["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    assert "PATH" in captured["env"]


# --- run_cpsat_python_file_checked -------------------------------------------


def _checked_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Write a valid model script and checker script; return both paths."""
    script = tmp_path / "model.py"
    script.write_text("print('ignored by mock')", encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text("print('ignored by mock')", encoding="utf-8")
    return script, checker


def _checked_result(
    status: str,
    *,
    solution: dict | None,
    timed_out: bool = False,
) -> CpsatPythonResult:
    result = CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution,
        objective=10,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=False,
        duration_ms=5,
    )
    result.diagnostic = cpsat_result_diagnostic(result)
    return result


def _checker_report(status: str) -> CpsatCheckerReport:
    report = CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[] if status == "accepted" else ["nope"],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=False,
        truncated=False,
    )
    report.diagnostic = checker_report_diagnostic(report)
    return report


def _patch_checked(
    monkeypatch: pytest.MonkeyPatch,
    run_result: CpsatPythonResult,
    checker_outcome: CpsatCheckerReport | Exception,
) -> list[dict[str, Any]]:
    """Stub the model run and the checker run; return the checker call log."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_cpsat_python_file",
        lambda script, **kw: run_result,
    )

    def _fake_run_checker_file(checker: Path, result: Any, **kw: Any) -> CpsatCheckerReport:
        calls.append({"checker": checker, "result": result, **kw})
        if isinstance(checker_outcome, Exception):
            raise checker_outcome
        return checker_outcome

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_checker_file", _fake_run_checker_file)
    return calls


def test_checked_run_forwards_args_and_env_to_the_model_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dropping `args=` or `env=` on the inner call would silently no-op a
    # seed/config replay while still returning a plausible-looking result.
    script, checker = _checked_pair(tmp_path)
    model_kw: dict[str, Any] = {}

    def _fake_run(script_path: Path, **kw: Any) -> CpsatPythonResult:
        model_kw.update(kw)
        return _checked_result("optimal", solution={"x": 1})

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.run_cpsat_python_file", _fake_run)
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.run_checker_file",
        lambda *args, **kw: _checker_report("accepted"),
    )

    run_cpsat_python_file_checked(
        script, checker, args=["data.json"], env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"}
    )

    assert model_kw["args"] == ["data.json"]
    assert model_kw["env"] == {"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"}


def test_checked_run_accepted_carries_the_checker_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.checker is not None
    assert result.checker.status == "accepted"


def test_checked_run_accepted_leaves_the_top_level_diagnostic_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.diagnostic is None


def test_checked_run_rejected_carries_the_rejected_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.checker is not None
    assert result.checker.status == "rejected"


def test_checked_run_rejected_but_optimal_sets_the_top_level_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D8: `diagnostic: null` is the clean-success signal, so an optimal run the
    # checker rejected must NOT come back with a null top-level diagnostic.
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.diagnostic is not None
    assert result.diagnostic.category == "checker_failed"


def test_checked_run_rejected_preserves_the_model_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("rejected")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.status == "optimal"
    assert result.solution == {"x": 1}


def test_checked_run_timeout_with_incumbent_runs_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D5: `timeout` IS a diagnostic-accept status, so a recovered incumbent is
    # still checkable.
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch,
        _checked_result("timeout", solution={"x": 1}, timed_out=True),
        _checker_report("accepted"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert len(calls) == 1
    assert result.checker is not None
    assert result.checker_skipped_reason is None


def test_checked_run_timeout_without_incumbent_skips_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other side of the D5 boundary: same status, no solution -> not checkable.
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch,
        _checked_result("timeout", solution=None, timed_out=True),
        _checker_report("accepted"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert calls == []
    assert result.checker is None
    assert result.checker_skipped_reason == "solution is missing or empty"


def test_checked_run_infeasible_skips_the_checker_naming_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("infeasible", solution=None), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert calls == []
    assert result.checker_skipped_reason == "status='infeasible'"


def test_checked_run_checker_infrastructure_error_yields_a_diagnosed_error_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D4: a post-run infrastructure failure (temp-file write, spawn) becomes an
    # `error` report — it must never discard the completed model result.
    script, checker = _checked_pair(tmp_path)
    _patch_checked(
        monkeypatch,
        _checked_result("optimal", solution={"x": 1}),
        OSError("no space left on device"),
    )

    result = run_cpsat_python_file_checked(script, checker)

    assert result.status == "optimal"
    assert result.checker is not None
    assert result.checker.status == "error"
    assert any("checker infrastructure error" in e for e in result.checker.errors)
    assert result.checker.diagnostic is not None
    assert result.checker.diagnostic.category == "checker_failed"


def test_checked_run_defaults_the_checker_timeout_to_the_run_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(script, checker, timeout_ms=12_345)

    assert calls[0]["timeout_ms"] == 12_345
    assert result.checker_timeout_ms == 12_345


def test_checked_run_explicit_checker_timeout_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    result = run_cpsat_python_file_checked(
        script, checker, timeout_ms=12_345, checker_timeout_ms=999
    )

    assert calls[0]["timeout_ms"] == 999
    assert result.checker_timeout_ms == 999


def test_checked_run_forwards_problem_to_the_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, checker = _checked_pair(tmp_path)
    calls = _patch_checked(
        monkeypatch, _checked_result("optimal", solution={"x": 1}), _checker_report("accepted")
    )

    run_cpsat_python_file_checked(script, checker, problem='{"jobs": []}')

    assert calls[0]["problem"] == '{"jobs": []}'


def test_checked_run_rejects_a_non_positive_checker_timeout(tmp_path: Path) -> None:
    script, checker = _checked_pair(tmp_path)

    with pytest.raises(ValueError, match="checker_timeout_ms must be positive"):
        run_cpsat_python_file_checked(script, checker, checker_timeout_ms=0)


def _assert_no_child_spawned(script: Path, checker: Path, match: str) -> None:
    """Both spawn helpers are mocked: a rejection must reach neither."""
    with (
        patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen,
        patch("openconstraint_mcp.pyexec.core.execute_child") as fake_execute,
        patch("openconstraint_mcp.pyexec.checker.execute_child") as fake_checker_execute,
    ):
        with pytest.raises(ValueError, match=match):
            run_cpsat_python_file_checked(script, checker)
    fake_popen.assert_not_called()
    fake_execute.assert_not_called()
    fake_checker_execute.assert_not_called()


def test_checked_run_invalid_checker_path_spawns_nothing(tmp_path: Path) -> None:
    script, _ = _checked_pair(tmp_path)
    _assert_no_child_spawned(script, tmp_path / "nope.py", r"checker_path does not exist")


def test_checked_run_invalid_script_path_spawns_nothing(tmp_path: Path) -> None:
    _, checker = _checked_pair(tmp_path)
    _assert_no_child_spawned(tmp_path / "nope.py", checker, r"script_path does not exist")


def test_inline_run_spawn_failure_returns_structured_error(tmp_path: Path) -> None:
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", timeout_ms=1000)
    assert result.status == "error"


def test_spawn_failure_reports_no_return_code(tmp_path: Path) -> None:
    # No child existed, so there is no exit status to report — never a synthesized code.
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", timeout_ms=1000)
    assert result.return_code is None


def test_spawn_failure_surfaces_the_os_error_in_stderr() -> None:
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(7, "Argument list too long"),
    ):
        result = run_cpsat_python("print(1)", timeout_ms=1000)
    assert "failed to start the Python child process" in result.stderr
    assert "Argument list too long" in result.stderr


def test_file_run_spawn_failure_returns_structured_error(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("print(1)", encoding="utf-8")
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(24, "Too many open files"),
    ):
        result = run_cpsat_python_file(script, timeout_ms=1000)
    assert result.status == "error"
    assert "Too many open files" in result.stderr


def test_spawn_failure_result_carries_a_diagnostic() -> None:
    # The error path must be as inspectable as any other result the tools return.
    with patch(
        "openconstraint_mcp.pyexec.core.execute_child",
        side_effect=ChildSpawnError(12, "Cannot allocate memory"),
    ):
        result = run_cpsat_python("print(1)", timeout_ms=1000)
    assert result.diagnostic is not None
