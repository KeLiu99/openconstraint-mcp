"""Shared process-group launch and tree-kill primitives.

Importable by both ``minizinc`` and ``pyexec`` without coupling those subtrees.
All dependencies are stdlib only.

Security posture: this module provides robustness boundaries (timeout + tree-kill),
not a security sandbox. It performs no network blocking, AST filtering, or syscall
restriction.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import Any

# Launch the cancellable child as the leader of its own process group/session so
# the *whole* tree (parent plus any subprocesses it forks) can be signalled at once.
# POSIX: a new session via ``start_new_session=True``, killed with ``os.killpg``.
# Windows: ``CREATE_NEW_PROCESS_GROUP`` (the attribute is only referenced on win32;
# child cleanup there is best-effort via ``taskkill /T`` — documented in
# ``terminate_process_tree``).
if sys.platform == "win32":
    START_NEW_SESSION: bool = False
    CREATION_FLAGS: int = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    START_NEW_SESSION = True
    CREATION_FLAGS = 0

# Grace period between SIGTERM and an escalated SIGKILL when terminating a tree.
_TERMINATE_GRACE_SECONDS: float = 4.0

# Public milliseconds view of the same SIGTERM→SIGKILL grace, for callers that must
# account for the time a timed-out child can spend being terminated (a future
# admission gate would budget two waits — once after SIGTERM, once after SIGKILL —
# per timed-out child; no current caller needs this, but it's kept public for the
# planned explicit-experiments tool). Exposed so such callers never import the
# private ``_TERMINATE_GRACE_SECONDS``.
PROCESS_TREE_TERMINATE_GRACE_MS: int = int(_TERMINATE_GRACE_SECONDS * 1000)


def process_tree_terminate_worst_case_ms() -> int:
    """Return the conservative termination wait budget for one timed-out tree.

    POSIX termination can wait once after SIGTERM and once after SIGKILL. Windows
    cleanup is different but this remains a safe upper budget for callers that
    need pre-flight admission estimates.
    """
    return 2 * PROCESS_TREE_TERMINATE_GRACE_MS


def popen_process_group(cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    """Launch ``cmd`` as the leader of its own process group.

    Passes ``start_new_session``/``creationflags`` appropriate for the current
    platform so callers do not duplicate the platform-dispatch logic.
    Extra keyword arguments are forwarded to ``subprocess.Popen``. The return
    type is ``Popen[Any]`` because the forwarded ``**kwargs`` decide the stream
    text/bytes mode and matches ``terminate_process_tree``.
    """
    return subprocess.Popen(
        cmd,
        start_new_session=START_NEW_SESSION,
        creationflags=CREATION_FLAGS,
        **kwargs,
    )


def _killpg(pgid: int, sig: int) -> None:
    """Signal a process group, swallowing the already-gone / not-permitted cases."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def terminate_process_tree(
    proc: subprocess.Popen[Any], *, grace_seconds: float = _TERMINATE_GRACE_SECONDS
) -> None:
    """Terminate ``proc`` and its child tree; idempotent.

    The parent process must have been launched as a process-group leader (via
    ``popen_process_group`` or equivalent) so the whole tree can be signalled.

    POSIX: a no-op only once the whole GROUP is gone — a leader that already
    exited (even reaped) can leave live descendants in its group, and those must
    still be swept. SIGTERM the group, then escalate to SIGKILL after
    ``grace_seconds`` if any group member (leader OR descendant) is still alive —
    a SIGTERM-resistant descendant must not survive its leader's quick exit.
    Windows: a handle that has already exited is a no-op; otherwise
    ``taskkill /T /F`` to tear down the whole tree while it is still intact,
    falling back to ``proc.terminate()`` if taskkill is unavailable —
    best-effort; guaranteed child cleanup may NOT match POSIX.
    """
    if sys.platform == "win32":
        if proc.poll() is not None:
            return
        terminate_process_tree_windows(proc, grace_seconds=grace_seconds)
    else:
        terminate_process_tree_posix(proc, grace_seconds=grace_seconds)


def terminate_process_tree_posix(proc: subprocess.Popen[Any], *, grace_seconds: float) -> None:
    # popen_process_group launched the child as its own session leader, so
    # proc.pid IS the group id for the group's whole lifetime — POSIX keeps it
    # reserved while any member survives. Never query os.getpgid for it: once
    # the leader is reaped its pid can be recycled, and getpgid on the recycled
    # pid would resolve an UNRELATED process's group and aim our signals at it.
    #
    # KNOWN, ACCEPTED residual race — do not "fix" without a genuinely better
    # design: if the whole group is already dead, the leader reaped (Popen.poll()
    # is the reaper), and the kernel recycles that exact pid as a NEW group
    # leader in the milliseconds before our killpg, the recycled group gets our
    # signals. The liveness probe below cannot tell that group from ours, and a
    # pidfd cannot help (pidfds signal single processes; Linux has no group
    # pidfd). The only real fix is deferring the reap — observing exit via
    # os.waitid(WNOWAIT) so the zombie leader keeps the pid reserved through the
    # sweep — which means reimplementing Popen's reaping for a window that
    # sequential pid allocation already makes practically unhittable.
    pgid = proc.pid
    if proc.poll() is not None and not _process_group_alive(pgid):
        # leader reaped and group empty — nothing left to signal
        return
    _killpg(pgid, signal.SIGTERM)
    # The leader exiting does not prove the tree is gone: a SIGTERM-resistant
    # descendant stays alive in the group, so escalation must check the GROUP,
    # not just the leader.
    if _group_exits_within(proc, pgid, grace_seconds):
        return
    _killpg(pgid, signal.SIGKILL)
    # SIGKILL is asynchronous: a descendant (re-parented to init, or one in an
    # uninterruptible syscall) can outlive the leader's reap. Wait on the GROUP,
    # not just the leader, so this returns only once the whole tree is gone or the
    # grace expires — mirroring the SIGTERM escalation above and honoring the
    # "no-op only once the whole group is gone" contract on the KILL path too. The
    # poll inside still reaps the leader, which is what the caller's tracker
    # unregister keys off, so it must not unregister while a group member survives.
    _group_exits_within(proc, pgid, grace_seconds)


_GROUP_POLL_INTERVAL_S: float = 0.05


def _group_exits_within(proc: subprocess.Popen[Any], pgid: int, grace_seconds: float) -> bool:
    """True if the leader was reaped AND the whole group vanished within the grace."""
    deadline = time.monotonic() + grace_seconds
    while True:
        if proc.poll() is not None and not _process_group_alive(pgid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_GROUP_POLL_INTERVAL_S, remaining))


def _process_group_alive(pgid: int) -> bool:
    """Whether any member of the process group still exists (zombies included)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else — still alive
    return True


def terminate_process_tree_windows(proc: subprocess.Popen[Any], *, grace_seconds: float) -> None:
    # taskkill /T /F tears down the whole tree (parent + solver children) while the
    # parent is still alive to anchor the tree walk. proc.terminate() (TerminateProcess)
    # reaches only the parent; once it is reaped, taskkill /T can no longer find the
    # orphaned solver children — so the tree kill must run FIRST, not as a post-wait
    # escalation. Fall back to terminating the parent only if taskkill is unavailable.
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=grace_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_seconds)
