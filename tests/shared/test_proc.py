"""Tests for the shared process-tree runner (proc.py)."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from openconstraint_mcp.shared.proc import (
    PROCESS_TREE_TERMINATE_GRACE_MS,
    popen_process_group,
    process_tree_terminate_worst_case_ms,
    terminate_process_tree,
    terminate_process_tree_windows,
)


class _FakePopen:
    """A subprocess.Popen stand-in exposing only what the process-tree killer reads.

    ``poll()`` returns ``None`` until a wait/kill sets ``returncode``, so
    ``terminate_process_tree`` treats a fresh handle as live.
    """

    def __init__(self, *, returncode: int = 0) -> None:
        self.pid = 4321
        self._final_rc = returncode
        self.returncode: int | None = None
        self.wait_calls = 0
        self.terminate_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        self.returncode = self._final_rc
        return self._final_rc

    def terminate(self) -> None:
        self.terminate_calls += 1


def test_process_tree_terminate_worst_case_ms_accounts_for_two_waits() -> None:
    assert process_tree_terminate_worst_case_ms() == 2 * PROCESS_TREE_TERMINATE_GRACE_MS


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_escalates_real_sigterm_ignored_child() -> None:
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = popen_process_group(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == "ready\n"

        grace_seconds = 0.05
        started = time.monotonic()
        terminate_process_tree(proc, grace_seconds=grace_seconds)
        elapsed_seconds = time.monotonic() - started

        assert proc.poll() == -signal.SIGKILL
        assert elapsed_seconds >= grace_seconds
        assert elapsed_seconds <= (2 * grace_seconds) + 1.0
    finally:
        terminate_process_tree(proc, grace_seconds=0.01)
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc, grace_seconds=0.01)
            proc.kill()
            proc.communicate(timeout=1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_signals_group_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePopen(returncode=0)  # poll() is None until wait/kill — a live handle
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("openconstraint_mcp.shared.proc.os.getpgid", lambda _pid: 5555)
    monkeypatch.setattr(
        "openconstraint_mcp.shared.proc.os.killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    terminate_process_tree(fake)  # type: ignore[arg-type]

    assert killed[0] == (5555, signal.SIGTERM)
    assert fake.wait_calls >= 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_is_noop_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An already-exited handle (poll() returns a code) must not be signalled again.
    fake = _FakePopen(returncode=0)
    fake.returncode = 0  # already terminal
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "openconstraint_mcp.shared.proc.os.killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    terminate_process_tree(fake)  # type: ignore[arg-type]

    assert killed == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_is_noop_when_group_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A race/cancel-before-start: the process is gone before getpgid, which raises
    # ProcessLookupError; termination degrades to a no-op rather than crashing.
    fake = _FakePopen(returncode=0)
    killed: list[tuple[int, int]] = []

    def _gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr("openconstraint_mcp.shared.proc.os.getpgid", _gone)
    monkeypatch.setattr(
        "openconstraint_mcp.shared.proc.os.killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    terminate_process_tree(fake)  # type: ignore[arg-type]

    assert killed == []


# The Windows branch is exercised directly (not via the platform-dispatching
# terminate_process_tree) so the control-flow regression is caught on the Linux CI
# host, where TerminateProcess/taskkill cannot run for real.
def test_terminate_process_tree_windows_kills_tree_via_taskkill_when_parent_exits_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: proc.terminate() (TerminateProcess) reaps only the parent, so
    # gating taskkill behind the parent OUTLIVING the grace window meant the happy
    # path returned having orphaned the solver child. taskkill /T /F must run while
    # the parent is alive to anchor the tree walk, not as a post-wait escalation.
    fake = _FakePopen(returncode=0)  # poll() is None → live; wait() returns at once
    run_calls: list[list[str]] = []

    def _fake_run(cmd: Any, **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        run_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("openconstraint_mcp.shared.proc.subprocess.run", _fake_run)

    terminate_process_tree_windows(fake, grace_seconds=0.01)  # type: ignore[arg-type]

    assert run_calls == [["taskkill", "/T", "/F", "/PID", "4321"]]
    assert fake.terminate_calls == 0  # whole-tree kill, not a parent-only terminate


def test_terminate_process_tree_windows_falls_back_to_terminate_when_taskkill_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If taskkill is missing/unrunnable (OSError), still attempt it first, then
    # degrade to terminating the parent rather than skipping teardown entirely.
    fake = _FakePopen(returncode=0)
    attempted: list[str] = []

    def _no_taskkill(_cmd: Any, **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        attempted.append("run")
        raise FileNotFoundError("taskkill not found")

    monkeypatch.setattr("openconstraint_mcp.shared.proc.subprocess.run", _no_taskkill)

    terminate_process_tree_windows(fake, grace_seconds=0.01)  # type: ignore[arg-type]

    assert attempted == ["run"]  # taskkill attempted before the fallback
    assert fake.terminate_calls == 1  # then fell back to the parent terminate
