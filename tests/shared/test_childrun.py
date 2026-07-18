"""Unit tests for the shared capped child executor — all subprocess calls mocked.

These cover the protocol-agnostic run loop (timeout, output cap, tree-kill,
tracker wiring, on_start lifecycle, env overlay) that ``pyexec`` and ``minizinc``
both build on. Protocol parsing (CP-SAT status/objective, MiniZinc stream) is
tested in the respective caller packages.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openconstraint_mcp.shared.childrun import (
    MAX_OUTPUT_BYTES,
    ChildExecutionResult,
    _read_capped,
    execute_child,
)

_ARGV = ["/usr/bin/true"]


class _SpyTracker:
    """Records register/unregister calls so wiring can be asserted without a kill."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register(self, proc: Any) -> None:
        self.events.append(("register", proc))

    def unregister(self, proc: Any) -> None:
        self.events.append(("unregister", proc))


def _execute(
    *,
    cwd: Path,
    stdout_content: str = "",
    stderr_content: str = "",
    returncode: int = 0,
    timeout: bool = False,
    large_output: bool = False,
    timeout_ms: int = 5000,
    tracker: Any = None,
    on_start: Any = None,
    env: dict[str, str | None] | None = None,
) -> tuple[ChildExecutionResult, MagicMock]:
    """Run execute_child with popen_process_group + terminate_process_tree patched.

    The fake writes the requested content into the executor's real capture files,
    so ``_read_capped`` reads back exactly what a child would have emitted. Returns
    the result together with the ``terminate_process_tree`` mock so tree-kill can be
    asserted (``ChildExecutionResult`` is frozen, so the mock can't ride on it).
    """

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None if (timeout or large_output) else returncode

        actual_stdout = "x" * (MAX_OUTPUT_BYTES + 1) if large_output else stdout_content
        stdout_file = kwargs.get("stdout")
        stderr_file = kwargs.get("stderr")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write(actual_stdout)
            stdout_file.flush()
        if stderr_file and hasattr(stderr_file, "write"):
            stderr_file.write(stderr_content)
            stderr_file.flush()

        if timeout:
            fake.poll = lambda: fake.returncode  # exits only when the mock kill reaps it
        else:
            _poll_count = [0]

            def _poll() -> int | None:
                _poll_count[0] += 1
                if large_output and _poll_count[0] < 2:
                    return None
                fake.returncode = returncode
                return returncode

            fake.poll = _poll

        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch(
            "openconstraint_mcp.shared.childrun.terminate_process_tree",
            side_effect=lambda proc: setattr(proc, "returncode", returncode),
        ) as mock_kill,
    ):
        result = execute_child(
            _ARGV, cwd, timeout_ms=timeout_ms, tracker=tracker, on_start=on_start, env=env
        )
    return result, mock_kill


def _capture_popen_kwargs(tmp_path: Path, *, env: dict[str, str | None] | None) -> dict[str, Any]:
    """Run execute_child with a fake popen and return the kwargs it received."""
    captured: dict[str, Any] = {}

    def _fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        fake.poll = lambda: 0
        return fake

    with (
        patch("openconstraint_mcp.shared.childrun.popen_process_group", side_effect=_fake_popen),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        execute_child(_ARGV, tmp_path, timeout_ms=1000, tracker=None, env=env)
    return captured


# --- result shape ------------------------------------------------------------


def test_execute_child_returns_captured_output(tmp_path: Path) -> None:
    result, _ = _execute(cwd=tmp_path, stdout_content="hello", stderr_content="warn", returncode=0)

    assert result.stdout == "hello"
    assert result.stderr == "warn"
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.truncated is False


# --- tracker wiring ----------------------------------------------------------


def test_execute_child_registers_then_unregisters_with_tracker(tmp_path: Path) -> None:
    tracker = _SpyTracker()

    _execute(cwd=tmp_path, tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]
    assert tracker.events[0][1] is tracker.events[1][1]  # same handle both times


def test_execute_child_unregisters_after_timeout_kill(tmp_path: Path) -> None:
    tracker = _SpyTracker()

    _execute(cwd=tmp_path, timeout=True, timeout_ms=50, tracker=tracker)

    # Even on the timeout path the killed child must leave the live set so the
    # lifespan never re-terminates a process that is already gone.
    assert [name for name, _ in tracker.events] == ["register", "unregister"]


# --- timeout / output cap ----------------------------------------------------


def test_execute_child_timeout_kills_tree_and_flags_timed_out(tmp_path: Path) -> None:
    result, mock_kill = _execute(cwd=tmp_path, timeout=True, timeout_ms=50)

    assert result.timed_out is True
    # Exactly one termination sequence — the finally's. The loop only flags and
    # breaks, so an unreapable child never pays two SIGTERM→SIGKILL grace windows
    # (which would overrun process_tree_terminate_worst_case_ms()'s 2x budget).
    assert mock_kill.call_count == 1


def _unreapable_popen_group(captured: dict[str, Any]) -> Any:
    """A fake that always polls live, modelling an unreapable D-state child."""

    def _fake_popen_group(_cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None  # never reaped
        fake.poll.return_value = None
        captured["proc"] = fake
        return fake

    return _fake_popen_group


def test_execute_child_final_reap_uses_non_blocking_poll_when_child_unreapable(
    tmp_path: Path,
) -> None:
    # Trigger: a child stuck in uninterruptible sleep (D state) that SIGKILL cannot
    # reap. terminate_process_tree gives up its bounded wait, so the final poll must
    # return without calling wait() or adding another grace period.
    captured: dict[str, Any] = {}
    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_unreapable_popen_group(captured),
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = execute_child(_ARGV, tmp_path, timeout_ms=50, tracker=None)

    assert result.timed_out is True
    assert result.return_code is None  # unreaped → no genuine code, but no hang
    captured["proc"].wait.assert_not_called()


def test_execute_child_keeps_unreaped_child_registered_for_teardown(tmp_path: Path) -> None:
    # A child that survives the kill unreaped (D state) must stay in the tracker so
    # the lifespan's terminate_all sweep can retry — unregistering a still-live
    # process would drop it from teardown coverage and leave it running.
    tracker = _SpyTracker()
    captured: dict[str, Any] = {}
    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_unreapable_popen_group(captured),
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        execute_child(_ARGV, tmp_path, timeout_ms=50, tracker=tracker)

    assert [name for name, _ in tracker.events] == ["register"]  # registered, not unregistered


def test_execute_child_tolerates_temp_dir_cleanup_failure(tmp_path: Path) -> None:
    # Windows regression: a child that outlives terminate_process_tree's bounded
    # wait still holds the capture files open, so removing the temp dir raises
    # PermissionError (you cannot delete a file another process has open). The run
    # must opt into best-effort cleanup so that error never propagates in place of
    # the result. Model CPython's real semantics — ignore_cleanup_errors=True
    # suppresses the failure inside TemporaryDirectory itself, not via a try/except
    # in execute_child — so the fake raises only when the flag was NOT requested.
    real_tempdir = tempfile.TemporaryDirectory

    class _CleanupFailsUnlessIgnored:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._ignored = bool(kwargs.pop("ignore_cleanup_errors", False))
            self._inner = real_tempdir(*args, **kwargs)

        def __enter__(self) -> str:
            return self._inner.__enter__()

        def __exit__(self, *exc: Any) -> bool:
            self._inner.__exit__(*exc)  # remove the real dir so the test leaks nothing
            if not self._ignored:
                raise PermissionError("capture file still held open by a live child")
            return False

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = 0
        fake.poll = lambda: 0
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.tempfile.TemporaryDirectory",
            _CleanupFailsUnlessIgnored,
        ),
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        result = execute_child(_ARGV, tmp_path, timeout_ms=1000, tracker=None)

    assert result.return_code == 0  # cleanup failure swallowed, result still returned


@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_execute_child_non_positive_timeout_raises_before_spawn(
    tmp_path: Path, timeout_ms: int
) -> None:
    with patch("openconstraint_mcp.shared.childrun.popen_process_group") as fake_popen:
        with pytest.raises(ValueError, match="timeout_ms must be positive"):
            execute_child(_ARGV, tmp_path, timeout_ms=timeout_ms, tracker=None)
    fake_popen.assert_not_called()


def test_execute_child_child_gets_no_stdin(tmp_path: Path) -> None:
    # Over stdio the server's stdin carries JSON-RPC, so the child must get DEVNULL
    # rather than inherit the protocol channel.
    captured = _capture_popen_kwargs(tmp_path, env=None)

    assert captured.get("stdin") == subprocess.DEVNULL


def test_execute_child_large_output_truncates_and_requests_termination(tmp_path: Path) -> None:
    result, mock_kill = _execute(cwd=tmp_path, large_output=True, timeout_ms=5000)

    assert result.truncated is True
    assert mock_kill.call_count == 1  # single termination — the finally's, not loop+finally


def test_execute_child_large_output_requests_termination_reports_truncation_killed(
    tmp_path: Path,
) -> None:
    # When the cap check requests tree termination against a child last seen running,
    # the result records it via truncation_killed: callers may then treat the exit
    # code as the executor's artifact and mask it (a plain `truncated` also covers a
    # burst writer that exited before the loop observed the overrun).
    result, mock_kill = _execute(cwd=tmp_path, large_output=True, timeout_ms=5000)

    assert result.truncation_killed is True
    assert mock_kill.called


def test_execute_child_fast_exit_large_output_flagged_truncated(tmp_path: Path) -> None:
    # A child that overran the cap but exited before the first poll is still flagged
    # truncated by the post-run on-disk size recompute.
    result, _ = _execute(cwd=tmp_path, stdout_content="x" * (MAX_OUTPUT_BYTES + 1), returncode=0)

    assert result.truncated is True


def test_execute_child_fast_exit_large_output_keeps_real_return_code(tmp_path: Path) -> None:
    # A burst writer that overran the cap but exited ON ITS OWN was not killed by
    # the poll loop (the finally group sweep still runs, but only against an
    # already-exited child): its genuine (possibly nonzero) exit code is real
    # information and must not be reported as an executor kill.
    result, _ = _execute(cwd=tmp_path, stdout_content="x" * (MAX_OUTPUT_BYTES + 1), returncode=3)

    assert result.truncated is True
    assert result.truncation_killed is False
    assert result.return_code == 3


def test_execute_child_clean_exit_still_sweeps_process_group(tmp_path: Path) -> None:
    # A leader that exits on its own can leave live descendants in its process
    # group; the executor must sweep the group on the clean-exit path too, not
    # only after a timeout or cap kill (the sweep no-ops once the group is gone).
    _, mock_kill = _execute(cwd=tmp_path, stdout_content="done", returncode=0)

    assert mock_kill.called


def test_execute_child_timeout_kill_is_not_a_truncation_kill(tmp_path: Path) -> None:
    # The deadline kill is reported via timed_out; truncation_killed stays reserved
    # for the output-cap branch so callers can tell the two kill reasons apart.
    result, _ = _execute(cwd=tmp_path, timeout=True, timeout_ms=50)

    assert result.timed_out is True
    assert result.truncation_killed is False


def test_execute_child_combined_output_capped_across_streams(tmp_path: Path) -> None:
    # A fast child can fill BOTH streams before the poll loop sees the overrun.
    # The advertised cap is on the combined stdout+stderr size, so the returned
    # result must never carry up to MAX_OUTPUT_BYTES from each stream (~2x cap).
    result, _ = _execute(
        cwd=tmp_path,
        stdout_content="x" * (MAX_OUTPUT_BYTES + 1),
        stderr_content="y" * (MAX_OUTPUT_BYTES + 1),
        returncode=0,
    )

    combined = len(result.stdout.encode()) + len(result.stderr.encode())
    assert combined <= MAX_OUTPUT_BYTES
    assert result.truncated is True


def test_execute_child_stderr_gets_remaining_combined_budget(tmp_path: Path) -> None:
    # Joint truncation is stdout-first: a small stdout leaves the rest of the
    # combined budget to stderr rather than zeroing it out.
    stdout_bytes = 100
    result, _ = _execute(
        cwd=tmp_path,
        stdout_content="x" * stdout_bytes,
        stderr_content="y" * (MAX_OUTPUT_BYTES * 2),
        returncode=0,
    )

    assert result.stdout == "x" * stdout_bytes
    assert len(result.stderr.encode()) == MAX_OUTPUT_BYTES - stdout_bytes
    assert result.truncated is True


def test_execute_child_timeout_over_cap_reports_truncated(tmp_path: Path) -> None:
    # A burst writer can overrun the cap with the deadline (checked first) firing
    # before the size check. The timeout result must still report truncated=True,
    # recomputed from the on-disk size. The clock is mocked so the deadline wins on
    # the first poll.
    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 1234
        fake.returncode = None
        stdout_file = kwargs.get("stdout")
        if stdout_file and hasattr(stdout_file, "write"):
            stdout_file.write("x" * (MAX_OUTPUT_BYTES + 1))
            stdout_file.flush()
        fake.poll = lambda: None  # never exits on its own
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
        # start, loop-now (past deadline), elapsed_ms.
        patch("openconstraint_mcp.shared.childrun.time.monotonic", side_effect=[0.0, 100.0, 100.0]),
    ):
        result = execute_child(_ARGV, tmp_path, timeout_ms=50, tracker=None)

    assert result.timed_out is True
    assert result.truncated is True


# --- _read_capped ------------------------------------------------------------


def test_read_capped_reads_at_most_cap_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cap is applied at the read boundary: an oversized file is read at most
    # MAX_OUTPUT_BYTES into memory (never slurped whole), yet the pre-cap size it
    # reports for the truncation check reflects the true on-disk length.
    big = tmp_path / "stdout.txt"
    big.write_bytes(b"x" * (MAX_OUTPUT_BYTES * 2))

    def _no_slurp(self: Path) -> bytes:
        raise AssertionError("read_bytes() slurps the whole file; read must be capped")

    monkeypatch.setattr(Path, "read_bytes", _no_slurp)

    text, size = _read_capped(big)

    assert len(text) == MAX_OUTPUT_BYTES  # output capped at the read boundary
    assert size == MAX_OUTPUT_BYTES * 2  # true pre-cap size, from stat


def test_read_capped_missing_file_returns_empty(tmp_path: Path) -> None:
    text, size = _read_capped(tmp_path / "does-not-exist.txt")

    assert text == ""
    assert size == 0


# --- on_start lifecycle ------------------------------------------------------


def test_execute_child_on_start_called_once_with_live_proc(tmp_path: Path) -> None:
    received: list[Any] = []

    _execute(cwd=tmp_path, on_start=lambda p: received.append(p))

    assert len(received) == 1
    assert received[0].pid == 1234


def test_execute_child_on_start_terminate_ends_run(tmp_path: Path) -> None:
    # Calling terminate_process_tree inside on_start kills the child; the loop then
    # observes the exit and returns.
    killed: list[Any] = []

    def _kill_it(proc: Any) -> None:
        from openconstraint_mcp.shared.childrun import terminate_process_tree

        terminate_process_tree(proc)
        killed.append(proc)

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 9999
        fake.returncode = None
        _calls = [0]

        def _poll() -> int | None:
            _calls[0] += 1
            if _calls[0] == 1:
                return None
            fake.returncode = -15
            return -15

        fake.poll = _poll
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree") as mock_kill,
    ):
        execute_child(_ARGV, tmp_path, timeout_ms=5000, tracker=None, on_start=_kill_it)

    assert mock_kill.called
    assert len(killed) == 1


def test_execute_child_on_start_raise_still_reaps_child(tmp_path: Path) -> None:
    # The callback fires inside the reaping guard, so a raising hook must not orphan
    # the process it was handed — the finally terminates and waits it.
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
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree") as mock_kill,
    ):
        with pytest.raises(RuntimeError, match="on_start failed"):
            execute_child(_ARGV, tmp_path, timeout_ms=5000, tracker=None, on_start=_boom)

    assert mock_kill.called


def test_execute_child_on_start_raise_still_unregisters_child(tmp_path: Path) -> None:
    # The companion to the reap invariant: register/unregister stay balanced even
    # when on_start blows up, so the lifespan never re-terminates a reaped process.
    tracker = _SpyTracker()

    def _boom(proc: Any) -> None:
        raise RuntimeError("on_start failed")

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 4242
        fake.returncode = None
        _calls = [0]

        def _poll() -> int | None:
            _calls[0] += 1
            if _calls[0] == 1:
                return None
            fake.returncode = -15
            return -15

        fake.poll = _poll
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch(
            "openconstraint_mcp.shared.childrun.terminate_process_tree",
            side_effect=lambda proc: proc.poll(),
        ),
    ):
        with pytest.raises(RuntimeError, match="on_start failed"):
            execute_child(_ARGV, tmp_path, timeout_ms=5000, tracker=tracker, on_start=_boom)

    assert [name for name, _ in tracker.events] == ["register", "unregister"]
    assert tracker.events[0][1] is tracker.events[1][1]  # same handle both times


def test_execute_child_register_raise_still_reaps_child(tmp_path: Path) -> None:
    # register() runs after the child is spawned; a raising register (e.g. a closed
    # tracker whose self-terminate hits a transient OS error) fires inside the
    # reaping guard, so the finally must terminate and wait the child rather than
    # orphan it.
    class _BoomTracker:
        def register(self, proc: Any) -> None:
            raise RuntimeError("register failed")

        def unregister(self, proc: Any) -> None:
            pass

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 4242
        fake.returncode = None  # live when register fires
        fake.poll = lambda: None
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree") as mock_kill,
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            execute_child(_ARGV, tmp_path, timeout_ms=5000, tracker=_BoomTracker())

    assert mock_kill.called  # finally terminated the child instead of orphaning it


def test_execute_child_register_raise_skips_unregister(tmp_path: Path) -> None:
    # register↔unregister stay paired: a child whose register never completed is not
    # in the live set, so the finally must reap it without unregistering a handle it
    # never added.
    class _RaisingRegisterTracker:
        def __init__(self) -> None:
            self.events: list[str] = []

        def register(self, proc: Any) -> None:
            self.events.append("register")
            raise RuntimeError("register failed")

        def unregister(self, proc: Any) -> None:
            self.events.append("unregister")

    tracker = _RaisingRegisterTracker()

    def _fake_popen_group(cmd: list[str], **kwargs: Any) -> MagicMock:
        fake = MagicMock()
        fake.pid = 4242
        fake.returncode = None
        fake.poll = lambda: None
        return fake

    with (
        patch(
            "openconstraint_mcp.shared.childrun.popen_process_group",
            side_effect=_fake_popen_group,
        ),
        patch("openconstraint_mcp.shared.childrun.terminate_process_tree"),
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            execute_child(_ARGV, tmp_path, timeout_ms=5000, tracker=tracker)

    assert tracker.events == ["register"]  # no unregister for a never-registered child


# --- env overlay -------------------------------------------------------------


def test_execute_child_env_overlay_merged_on_top_of_parent(tmp_path: Path) -> None:
    captured = _capture_popen_kwargs(tmp_path, env={"OPENCONSTRAINT_MCP_CPSAT_SEED": "7"})

    env = captured["env"]
    assert env is not None
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    # The child still inherits the parent's environment (overlay, not replacement).
    assert "PATH" in env


def test_execute_child_no_env_overlay_leaves_child_environment_inherited(tmp_path: Path) -> None:
    # env=None must pass env=None to Popen so the child inherits os.environ as before.
    captured = _capture_popen_kwargs(tmp_path, env=None)
    assert captured["env"] is None


def test_execute_child_env_none_value_deletes_inherited_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A key mapped to None in the overlay must be removed from the inherited
    # environment, so a stale value the parent process inherited cannot leak into
    # the child.
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", "/stale/leftover-config.json")

    captured = _capture_popen_kwargs(
        tmp_path,
        env={"OPENCONSTRAINT_MCP_CPSAT_CONFIG": None, "OPENCONSTRAINT_MCP_CPSAT_SEED": "7"},
    )

    env = captured["env"]
    assert env is not None
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" not in env
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "7"
    # Unrelated inherited variables are untouched.
    assert "PATH" in env
