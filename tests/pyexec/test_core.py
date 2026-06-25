"""Unit tests for pyexec/core.py — all subprocess calls mocked."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openconstraint_mcp.pyexec.core import (
    MAX_OUTPUT_BYTES,
    CpsatPythonResult,
    _read_capped,
    run_cpsat_python,
)

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
        patch("openconstraint_mcp.pyexec.core.popen_process_group", side_effect=_fake_popen_group),
        patch("openconstraint_mcp.pyexec.core.terminate_process_tree") as mock_kill,
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
    with patch("openconstraint_mcp.pyexec.core.popen_process_group") as fake_popen:
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
            "openconstraint_mcp.pyexec.core.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.core.terminate_process_tree"),
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
            "openconstraint_mcp.pyexec.core.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.core.terminate_process_tree"),
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
            "openconstraint_mcp.pyexec.core.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.core.terminate_process_tree"),
        # start, loop-now (past deadline), elapsed_ms.
        patch("openconstraint_mcp.pyexec.core.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
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
            "openconstraint_mcp.pyexec.core.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.pyexec.core.terminate_process_tree"),
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
