"""Tests for the shared process-tree runner (proc.py)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
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


def test_popen_process_group_launches_leader_with_platform_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole-tree-killable launch contract, asserted where the flags are owned:
    # POSIX children get start_new_session=True (a new session leader, killable via
    # os.killpg); Windows children get CREATE_NEW_PROCESS_GROUP. Consumers (the
    # MiniZinc and CP-SAT runners) patch popen_process_group itself, so the raw
    # values are pinned independently here, alongside the caller kwarg forwarding.
    calls: list[dict[str, Any]] = []
    handle = _FakePopen()

    def _fake_popen(cmd: Any, **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return handle

    monkeypatch.setattr("openconstraint_mcp.shared.proc.subprocess.Popen", _fake_popen)

    returned = popen_process_group(["some-binary", "--flag"], cwd="/work", text=True)

    assert returned is handle
    assert calls[0]["cmd"] == ["some-binary", "--flag"]
    kwargs = calls[0]["kwargs"]
    assert kwargs["cwd"] == "/work"
    assert kwargs["text"] is True
    if sys.platform == "win32":
        assert kwargs["start_new_session"] is False
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
        assert kwargs["creationflags"] == 0


def test_process_tree_terminate_worst_case_ms_accounts_for_two_waits() -> None:
    assert process_tree_terminate_worst_case_ms() == 2 * PROCESS_TREE_TERMINATE_GRACE_MS


@pytest.mark.integration
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


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_kills_sigterm_resistant_descendant_after_leader_exit() -> None:
    # The leader dies on SIGTERM at once, but its descendant (same process group)
    # ignores SIGTERM. Escalation must key off the GROUP, not the leader: the
    # leader's quick exit must not skip the SIGKILL the descendant still needs.
    grandchild_script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('grandchild-ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    leader_script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])\n"
        "print(f'grandchild-pid {child.pid}', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = popen_process_group(
        [sys.executable, "-c", leader_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    grandchild_pid: int | None = None
    try:
        assert proc.stdout is not None
        # Both processes write to the shared pipe: one pid line from the leader,
        # one ready line from the grandchild (order not guaranteed).
        lines = [proc.stdout.readline(), proc.stdout.readline()]
        for line in lines:
            if line.startswith("grandchild-pid "):
                grandchild_pid = int(line.split()[1])
        assert grandchild_pid is not None
        assert "grandchild-ready\n" in lines

        terminate_process_tree(proc, grace_seconds=0.2)

        assert proc.poll() is not None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _pid_dead_or_zombie(grandchild_pid):
            time.sleep(0.05)
        assert _pid_dead_or_zombie(grandchild_pid), "SIGTERM-resistant descendant survived"
    finally:
        if grandchild_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(grandchild_pid, signal.SIGKILL)
        terminate_process_tree(proc, grace_seconds=0.01)
        proc.communicate(timeout=1)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_sweeps_descendants_after_normal_leader_exit() -> None:
    # The leader exits on its own (code 0) leaving a descendant alive in the
    # group, and the parent has already reaped it — the state execute_child's
    # finally block hands over after a clean exit. The sweep must not treat the
    # reaped leader as proof the tree is gone: proc.pid is still the group id.
    grandchild_script = "import time\nprint('grandchild-ready', flush=True)\ntime.sleep(60)\n"
    leader_script = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])\n"
        "print(f'grandchild-pid {child.pid}', flush=True)\n"
    )
    proc = popen_process_group(
        [sys.executable, "-c", leader_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    grandchild_pid: int | None = None
    try:
        assert proc.stdout is not None
        lines = [proc.stdout.readline(), proc.stdout.readline()]
        for line in lines:
            if line.startswith("grandchild-pid "):
                grandchild_pid = int(line.split()[1])
        assert grandchild_pid is not None
        assert "grandchild-ready\n" in lines
        assert proc.wait(timeout=5) == 0  # leader reaped before the sweep

        terminate_process_tree(proc, grace_seconds=0.2)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _pid_dead_or_zombie(grandchild_pid):
            time.sleep(0.05)
        assert _pid_dead_or_zombie(grandchild_pid), "descendant survived normal leader exit"
    finally:
        if grandchild_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(grandchild_pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=1)


def _pid_dead_or_zombie(pid: int) -> bool:
    """True once ``pid`` no longer runs: fully gone, or a zombie awaiting init's reap."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_signals_group_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The group id is proc.pid BY LAUNCH CONTRACT (session leader), never looked
    # up via os.getpgid: once a leader is reaped its pid can be recycled, and a
    # getpgid on the recycled pid would resolve an UNRELATED process's group and
    # aim our signals at it.
    fake = _FakePopen(returncode=0)  # poll() is None until wait/kill — a live handle
    killed: list[tuple[int, int]] = []

    def _must_not_query(_pid: int) -> int:
        raise AssertionError("terminate must not query os.getpgid (pid-reuse hazard)")

    monkeypatch.setattr("openconstraint_mcp.shared.proc.os.getpgid", _must_not_query)
    monkeypatch.setattr(
        "openconstraint_mcp.shared.proc.os.killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    # Small grace: the fake's "group" never dies (the mocked killpg always
    # succeeds), so the group-liveness poll runs the full window before SIGKILL.
    terminate_process_tree(fake, grace_seconds=0.05)  # type: ignore[arg-type]

    assert killed[0] == (4321, signal.SIGTERM)
    assert (4321, signal.SIGKILL) in killed  # group still alive → escalated
    assert fake.wait_calls >= 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_sweeps_group_when_leader_terminal_but_group_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A leader that exited (even reaped — proc.pid stays the group id by launch
    # contract) can leave live descendants in its group — those must still be
    # signalled, not skipped because the leader looks terminal.
    fake = _FakePopen(returncode=0)
    fake.returncode = 0  # leader already terminal
    signalled: list[tuple[int, int]] = []

    def _alive_group(pgid: int, sig: int) -> None:
        if sig:  # record deliveries, not the signal-0 liveness probes
            signalled.append((pgid, sig))

    monkeypatch.setattr("openconstraint_mcp.shared.proc.os.killpg", _alive_group)

    # Small grace: the mocked group never dies, so escalation must follow.
    terminate_process_tree(fake, grace_seconds=0.05)  # type: ignore[arg-type]

    assert signalled[0] == (4321, signal.SIGTERM)
    assert (4321, signal.SIGKILL) in signalled  # group still alive → escalated


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group termination")
def test_terminate_process_tree_is_noop_when_group_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Leader terminal AND its whole group empty (the signal-0 probe raises):
    # nothing is left to signal, so no TERM/KILL is delivered.
    fake = _FakePopen(returncode=0)
    fake.returncode = 0  # already terminal
    signalled: list[tuple[int, int]] = []

    def _gone_group(pgid: int, sig: int) -> None:
        if sig:  # record would-be deliveries before reporting the group gone
            signalled.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr("openconstraint_mcp.shared.proc.os.killpg", _gone_group)

    terminate_process_tree(fake)  # type: ignore[arg-type]

    assert signalled == []


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
