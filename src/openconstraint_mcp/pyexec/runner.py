"""Python-specific argv builder for CP-SAT child scripts.

The protocol-agnostic timeout / output-cap / process-tree-kill run loop now
lives in ``shared.childrun`` (importable by both ``pyexec`` and ``minizinc``);
this module keeps only the CP-SAT-specific concern: how to launch a Python
script under the server's own interpreter. ``core.py`` and ``checker.py``
import ``execute_child`` / ``ChildExecutionResult`` from ``shared.childrun``
directly.
"""

from __future__ import annotations

import sys
from pathlib import Path


def python_script_argv(script: Path) -> list[str]:
    # -u: unbuffered child stdout/stderr so prints reach the capture files as
    # they happen (not on a full buffer). This is what lets a flushed
    # intermediate result block survive a timeout kill (see core's partial
    # recovery on the timeout path).
    return [sys.executable, "-u", str(script)]
