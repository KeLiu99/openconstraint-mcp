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
_TERMINATE_GRACE_SECONDS: float = 3.0

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
    text/bytes mode (the sole caller routes stdout/stderr to files, so the
    handle carries no readable streams) and matches ``terminate_process_tree``.
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
    A handle that has already exited is a no-op.

    POSIX: SIGTERM the group, then escalate to SIGKILL after ``grace_seconds`` if
    the leader is still alive. Windows: ``taskkill /T /F`` to tear down the whole
    tree while it is still intact, falling back to ``proc.terminate()`` if taskkill
    is unavailable — best-effort; guaranteed child cleanup may NOT match POSIX.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        terminate_process_tree_windows(proc, grace_seconds=grace_seconds)
    else:
        terminate_process_tree_posix(proc, grace_seconds=grace_seconds)


def terminate_process_tree_posix(proc: subprocess.Popen[Any], *, grace_seconds: float) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return  # already reaped — idempotent no-op
    _killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        _killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_seconds)


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
