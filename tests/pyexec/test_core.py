"""Unit tests for pyexec/core.py — all subprocess calls mocked."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openconstraint_mcp.pyexec.core import (
    effective_checker_timeout_ms,
    run_cpsat_python,
    run_cpsat_python_file,
    seed_config_env,
    validate_checker_args,
    validate_cpsat_random_seed,
)
from openconstraint_mcp.pyexec.core import (
    normalize_objective as _normalize_objective,
)
from openconstraint_mcp.pyexec.runner import MAX_OUTPUT_BYTES, _read_capped
from openconstraint_mcp.schemas.cpsat import CpsatPythonResult

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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree") as mock_kill,
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


def test_run_cpsat_python_registers_then_unregisters_child_with_tracker() -> None:
    tracker = _SpyTracker()

    _run_with_mocked_proc(tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]
    assert tracker.events[0][1] is tracker.events[1][1]  # same handle both times


def test_run_cpsat_python_unregisters_child_after_timeout_kill() -> None:
    tracker = _SpyTracker()

    _run_with_mocked_proc(timeout=True, timeout_ms=100, tracker=tracker)

    # Even on the timeout path the killed child must leave the live set so the
    # lifespan never re-terminates a process that is already gone.
    assert [name for name, _ in tracker.events] == ["register", "unregister"]


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
# the MiniZinc path's _validate_model_and_timeout.
@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_run_cpsat_python_non_positive_timeout_raises(timeout_ms: int) -> None:
    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        run_cpsat_python("print('hi')", timeout_ms=5000)

    cmd = captured["cmd"]
    assert cmd[0] == sys.executable
    assert cmd[1] == "-u"  # unbuffered, before the script path


# (c2b) the child must not inherit the server's stdin: over stdio that channel
# carries JSON-RPC, so an input()/sys.stdin read would steal protocol bytes.
def test_run_cpsat_python_child_gets_no_stdin() -> None:
    captured: dict[str, Any] = {}

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        run_cpsat_python("print('hi')", timeout_ms=5000)

    assert captured.get("stdin") == subprocess.DEVNULL


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


# (c6) a burst writer can overrun the cap with the deadline firing before the
# loop's size check (the deadline is checked first). The timeout result must still
# report truncated=True, computed from the on-disk size like the clean-exit path.
# The clock is mocked so the deadline wins deterministically on the first poll.
def test_run_cpsat_python_timeout_over_cap_reports_truncated() -> None:
    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write("x" * (MAX_OUTPUT_BYTES + 1))
            stdout_file.flush()
        fake.poll = lambda: None  # never exits on its own
        fake.wait.return_value = None
        return fake

    with (
        patch(
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
        # start, loop-now (past deadline), elapsed_ms.
        patch("openconstraint_mcp.pyexec.runner.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
    ):
        result = run_cpsat_python("print('hi')", timeout_ms=50)

    assert result.timed_out is True
    assert result.status == "timeout"
    assert result.truncated is True


# (d) unparseable stdout → status="error"
def test_run_cpsat_python_unparseable_stdout_yields_error() -> None:
    result = _run_with_mocked_proc(stdout_content="not json at all")

    assert result.status == "error"
    assert result.solution is None


# (e) output over MAX_OUTPUT_BYTES → truncated=True, tree-kill invoked
def test_run_cpsat_python_large_output_truncates_and_kills() -> None:
    result = _run_with_mocked_proc(large_output=True, timeout_ms=5000)

    assert result.truncated is True
    assert result._mock_kill.called  # type: ignore[attr-defined]


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


# (h) non-numeric objective → coerced to None, status still parsed
def test_run_cpsat_python_non_numeric_objective_becomes_none() -> None:
    payload = json.dumps({"status": "optimal", "objective": "lots", "solution": {"x": 1}})
    result = _run_with_mocked_proc(stdout_content=payload)

    assert result.status == "optimal"
    assert result.objective is None
    assert result.solution == {"x": 1}


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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        result = run_cpsat_python("print('hi')", timeout_ms=5000)

    assert result.truncated is True
    assert result.status == "error"


# (j) _read_capped applies the cap at the read boundary: an oversized file is read
# at most MAX_OUTPUT_BYTES into memory (never slurped whole), yet the pre-cap size
# it reports for the truncation check reflects the true on-disk length.
def test_read_capped_reads_at_most_cap_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "stdout.txt"
    big.write_bytes(b"x" * (MAX_OUTPUT_BYTES * 2))

    def _no_slurp(self: Path) -> bytes:
        raise AssertionError("read_bytes() slurps the whole file; read must be capped")

    monkeypatch.setattr(Path, "read_bytes", _no_slurp)

    text, size = _read_capped(big)

    assert len(text) == MAX_OUTPUT_BYTES  # output capped at the read boundary
    assert size == MAX_OUTPUT_BYTES * 2  # true pre-cap size, from stat


# (j2) a missing/unreadable capture file degrades to empty output and zero length.
def test_read_capped_missing_file_returns_empty(tmp_path: Path) -> None:
    text, size = _read_capped(tmp_path / "does-not-exist.txt")

    assert text == ""
    assert size == 0


# --- run_cpsat_python_file: path-based variant -----------------------------


def _run_file_with_mocked_proc(
    script_path: Path,
    *,
    stdout_content: str = _VALID_STDOUT,
    returncode: int = 0,
    timeout_ms: int = 5000,
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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        result = run_cpsat_python_file(script_path, timeout_ms=timeout_ms, tracker=tracker, env=env)
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

    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="does not exist"):
            run_cpsat_python_file(missing)
    fake_popen.assert_not_called()


# --- on_start hook -----------------------------------------------------------


def test_run_cpsat_python_on_start_called_once_with_live_proc() -> None:
    """on_start receives the Popen handle exactly once right after launch."""
    received: list[Any] = []

    def _capture(proc: Any) -> None:
        received.append(proc)

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        run_cpsat_python("print('hi')", timeout_ms=5000, on_start=_capture)

    assert len(received) == 1
    assert received[0].pid == 1234


def test_run_cpsat_python_on_start_terminate_ends_run() -> None:
    """Calling terminate_process_tree inside on_start kills the child."""
    killed: list[Any] = []

    def _kill_it(proc: Any) -> None:
        from openconstraint_mcp.pyexec.runner import terminate_process_tree

        terminate_process_tree(proc)
        killed.append(proc)

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 9999
        fake.returncode = None  # simulate live
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write("")
            stdout_file.flush()
        # poll returns None first (live), then non-zero after the kill
        _calls = [0]

        def _poll() -> int | None:
            _calls[0] += 1
            if _calls[0] == 1:
                return None
            fake.returncode = -15
            return -15

        fake.poll = _poll
        fake.wait.return_value = -15
        return fake

    with (
        patch(
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree") as mock_kill,
    ):
        run_cpsat_python("print('hi')", timeout_ms=5000, on_start=_kill_it)

    assert mock_kill.called
    assert len(killed) == 1


def test_run_cpsat_python_on_start_raise_still_reaps_child() -> None:
    """If on_start raises, the finally still kills and reaps the live child.

    The callback fires inside the reaping guard, so a raising hook must not
    orphan the process it was handed — the finally terminates and waits it.
    """

    def _boom(proc: Any) -> None:
        raise RuntimeError("on_start failed")

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 4242
        fake.returncode = None  # live when on_start fires
        _calls = [0]

        def _poll() -> int | None:
            _calls[0] += 1
            if _calls[0] == 1:
                return None  # still running at the finally's reap check
            fake.returncode = -15
            return -15

        fake.poll = _poll
        fake.wait.return_value = -15
        return fake

    with (
        patch(
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree") as mock_kill,
    ):
        with pytest.raises(RuntimeError, match="on_start failed"):
            run_cpsat_python("print('hi')", timeout_ms=5000, on_start=_boom)

    # The reaping finally must have terminated the still-live child.
    assert mock_kill.called


def test_run_cpsat_python_on_start_raise_still_unregisters_child() -> None:
    """on_start raising must not leak the child from the tracker's live set.

    The kill-on-raise invariant is covered above; this pins the companion
    half — register/unregister are balanced even when on_start blows up, so the
    lifespan never re-terminates an already-reaped process.
    """
    tracker = _SpyTracker()

    def _boom(proc: Any) -> None:
        raise RuntimeError("on_start failed")

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 4242
        fake.returncode = None  # live when on_start fires
        _calls = [0]

        def _poll() -> int | None:
            _calls[0] += 1
            if _calls[0] == 1:
                return None  # still running at the finally's reap check
            fake.returncode = -15
            return -15

        fake.poll = _poll
        fake.wait.return_value = -15
        return fake

    with (
        patch(
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        with pytest.raises(RuntimeError, match="on_start failed"):
            run_cpsat_python("print('hi')", timeout_ms=5000, on_start=_boom, tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]
    assert tracker.events[0][1] is tracker.events[1][1]  # same handle both times


def test_run_cpsat_python_no_on_start_default_is_none() -> None:
    """Omitting on_start (default None) behaves identically to the old API."""
    result = _run_with_mocked_proc()

    assert result.status == "optimal"


def test_validate_script_path_unreadable_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError (e.g. unreadable file) is translated to ValueError, not leaked raw.

    So the tool's @_as_mcp_error(ValueError, ...) wrapper turns a mode-000 script
    into an actionable client message instead of an opaque traceback.
    """
    from openconstraint_mcp.pyexec.core import _validate_script_path

    script = tmp_path / "secret.py"
    script.write_text("print('x')", encoding="utf-8")

    def _boom(*_a: Any, **_k: Any) -> str:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(ValueError, match="not readable"):
        _validate_script_path(script)


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
            "openconstraint_mcp.pyexec.runner.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        run_cpsat_python_file(script, timeout_ms=5000, on_start=lambda p: received.append(p))

    assert len(received) == 1
    assert received[0].pid == 7777


# (k5) a directory is not a runnable script.
def test_run_cpsat_python_file_directory_raises(tmp_path: Path) -> None:
    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not a file"):
            run_cpsat_python_file(tmp_path)
    fake_popen.assert_not_called()


# (k6) an empty/whitespace-only script is rejected with a clear error.
def test_run_cpsat_python_file_empty_file_raises(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text("   \n", encoding="utf-8")

    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="is empty"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k7) a non-UTF-8 file surfaces a clear ValueError, not an opaque decode traceback.
def test_run_cpsat_python_file_non_utf8_raises(tmp_path: Path) -> None:
    script = tmp_path / "latin1.py"
    script.write_bytes(b"print('caf\xe9')")

    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="not valid UTF-8"):
            run_cpsat_python_file(script)
    fake_popen.assert_not_called()


# (k8) a non-positive timeout is rejected before any child is spawned.
@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_run_cpsat_python_file_non_positive_timeout_raises(tmp_path: Path, timeout_ms: int) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    with patch("openconstraint_mcp.pyexec.runner.popen_process_group") as fake_popen:
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
        patch("openconstraint_mcp.pyexec.runner.popen_process_group", side_effect=_fake_popen),
        patch("openconstraint_mcp.pyexec.runner.terminate_process_tree"),
    ):
        run_cpsat_python(source, timeout_ms=1000, env=env)
    return captured["env"]


def test_env_overlay_merged_on_top_of_parent_environment() -> None:
    env = _capture_popen_env("print('x')", env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"})
    assert env is not None
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    # The child still inherits the parent's environment (overlay, not replacement).
    assert "PATH" in env


def test_no_env_overlay_leaves_child_environment_inherited() -> None:
    # env=None must pass env=None to Popen so the child inherits os.environ as before.
    assert _capture_popen_env("print('x')", env=None) is None


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
