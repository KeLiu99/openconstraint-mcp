"""Tests for the shared in-flight child-process tracker (childproc.py)."""

from __future__ import annotations

import sys

import pytest

from openconstraint_mcp.shared.childproc import ChildProcessTracker
from openconstraint_mcp.shared.proc import popen_process_group


class _FakePopen:
    """A subprocess.Popen stand-in recording whether it was asked to terminate.

    ``poll()`` returns ``None`` (a live handle) so ``terminate_process_tree``
    would act on it; the tracker tests only need identity + the terminate hook.
    """

    def __init__(self) -> None:
        self.pid = 1234
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@pytest.mark.integration
def test_terminate_all_kills_a_registered_child() -> None:
    # The reproducing case: a live child registered with the tracker is killed
    # when the lifespan tears the tracker down, instead of being orphaned.
    child = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker = ChildProcessTracker()
    tracker.register(child)

    tracker.terminate_all()

    assert child.wait(timeout=5) is not None
    assert child.poll() is not None


def test_track_context_manager_registers_then_unregisters() -> None:
    tracker = ChildProcessTracker()
    fake = _FakePopen()

    with tracker.track(fake):  # type: ignore[arg-type]
        assert fake in tracker.snapshot()

    assert fake not in tracker.snapshot()


def test_terminate_all_skips_an_unregistered_child() -> None:
    # A child whose work finished and was unregistered must not be terminated.
    tracker = ChildProcessTracker()
    terminated: list[object] = []
    fake = _FakePopen()
    tracker.register(fake)
    tracker.unregister(fake)

    tracker.terminate_all(_terminator=terminated.append)

    assert terminated == []


def test_terminate_all_invokes_terminator_for_each_registered_child() -> None:
    tracker = ChildProcessTracker()
    terminated: list[object] = []
    first, second = _FakePopen(), _FakePopen()
    tracker.register(first)
    tracker.register(second)

    tracker.terminate_all(_terminator=terminated.append)

    assert set(map(id, terminated)) == {id(first), id(second)}


@pytest.mark.integration
def test_register_after_terminate_all_kills_the_late_child() -> None:
    # A worker still in the launch->register window when teardown fired must not
    # leak its child into a set nobody drains again: once terminate_all has closed
    # the tracker, a late registrant is terminated on the spot instead of orphaned
    # -- the same launch-window guard JobRegistry.shutdown applies via
    # cancel_requested.
    tracker = ChildProcessTracker()
    tracker.terminate_all()  # teardown has run; the tracker is now closed

    late = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker.register(late)

    assert late.poll() is not None  # killed at register, not left running
    assert late not in tracker.snapshot()  # and not added to the drained set
