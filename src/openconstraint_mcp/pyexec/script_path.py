"""Caller-supplied Python script path validation for the CP-SAT execution path.

Stdlib-only leaf: imports nothing from this project, so both the orchestrator
(``core.py``, validating ``script_path``) and the checker leaf (``checker.py``,
validating ``checker_path``) can use it without a sibling-to-sibling dependency
on each other.

The validator is parameterized by the caller-facing parameter name so every
rejection message names the argument the client actually passed
(``checker_path does not exist: ...``), which is what makes the message
actionable at the MCP boundary.
"""

from __future__ import annotations

from pathlib import Path


def validate_script_path(path: Path, *, parameter: str = "script_path") -> Path:
    """Resolve and validate a Python script path before any subprocess.

    Mirrors the MiniZinc path tools' contract (``validate_model_data_paths``):
    resolve to an absolute path (following a symlink the caller named), then
    reject a missing or non-regular file, and an empty/whitespace-only or
    non-UTF-8 script, with a clear ``ValueError`` naming both ``parameter`` and
    the offending path. The resolved path is returned so the caller uses the
    same path for argv and its parent for ``cwd`` — a relative input can't then
    double-count its subdir.
    """
    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"{parameter} does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"{parameter} is not a file: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{parameter} is not valid UTF-8: {resolved}") from exc
    except OSError as exc:
        raise ValueError(f"{parameter} is not readable: {resolved} ({exc})") from exc
    if not text.strip():
        raise ValueError(f"{parameter} file is empty: {resolved}")
    return resolved
