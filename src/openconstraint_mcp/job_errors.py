"""Shared job-submission error, dependency-light so both job registries can import it.

``JobRejectedError`` was previously in ``jobs.py``, which imports ``minizinc.core``.
Moving it here lets ``pyexec/jobs.py`` raise the same type without pulling the
MiniZinc path into the CP-SAT subtree — per AGENTS.md ("extract a shared primitive to a
dependency-light leaf both import", precedent ``runtime_install/errors.py``).
"""

from __future__ import annotations


class JobRejectedError(RuntimeError):
    """Raised when a submit would exceed the bounded running+queued capacity."""
