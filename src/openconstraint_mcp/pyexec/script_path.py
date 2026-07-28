"""Caller-supplied script path and child-argv validation for the CP-SAT path.

Stdlib-only leaf: imports nothing from this project, so both the orchestrator
(``core.py``, validating ``script_path``) and the checker leaf (``checker.py``,
validating ``checker_path``) can use it without a sibling-to-sibling dependency
on each other.

Every validator is parameterized by the caller-facing parameter name so every
rejection message names the argument the client actually passed
(``checker_path does not exist: ...``), which is what makes the message
actionable at the MCP boundary.
"""

from __future__ import annotations

from pathlib import Path

# `Popen` also rejects argv on SIZE, not just content, and every platform draws
# the line differently: Linux caps a SINGLE argument at MAX_ARG_STRLEN (32 pages
# = 128 KiB) and the whole argv+environ block at ARG_MAX, macOS caps argv+environ
# at 256 KiB, and Windows caps the composed command line at 32767 characters.
# This bound is a round 32 KiB — the same order as the tightest of those, and one
# byte above Windows' — applied to the combined UTF-8 encoding of `args`,
# because `args` is a flag/path list, not a data channel — a script's
# bulk input belongs in a file the script opens, which is also the only form the
# 1 MiB child-output cap and the save path's replay can handle.
#
# It is a CONSERVATIVE HEURISTIC, not a reproduction of any OS limit: the real
# ceiling also counts the interpreter path, the script path, and the inherited
# environment, none of which this function is given. It shrinks the spawn-failure
# window to inputs no legitimate caller sends; it does not close it.
MAX_CHILD_ARGV_BYTES: int = 32 * 1024


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
    if "\0" in str(path):
        raise ValueError(f"{parameter} contains a NUL character: {path!r}")
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


def validate_script_args(args: list[str] | None, *, parameter: str = "args") -> None:
    """Reject child ``sys.argv[1:]`` entries that cannot survive a spawn.

    ``subprocess.Popen`` rejects argv at SPAWN time rather than at
    argument-validation time, in two ways Pydantic's ``list[str]`` does not
    already exclude: an embedded NUL raises ``ValueError: embedded null byte``,
    and an oversized argv raises ``OSError(E2BIG)`` — the latter from a single
    argument over the per-argument cap, not only from a large total. Callers
    that validate up front — the experiment's before-ANY-attempt pass, the job
    registry's before-admission pass — need both rejections to happen in their
    own preflight, or an already-spawned child (or an already-created job
    record) outlives a request that was invalid from the start. An E2BIG raised
    mid-run is the worse of the two: it surfaces as a raw ``OSError`` rather
    than a structured result, after earlier attempts have already executed.

    The size bound is deliberately conservative and cannot be exact (see
    ``MAX_CHILD_ARGV_BYTES``), so it makes an oversized argv a structured
    rejection for any plausible caller without claiming a spawn can never fail
    on size. ``None`` and ``[]`` are valid — they mean "no arguments".
    """
    total_bytes = 0
    for index, arg in enumerate(args or ()):
        if "\0" in arg:
            raise ValueError(
                f"{parameter}[{index}] contains a NUL character, which cannot be "
                "passed to a child process"
            )
        # Counted per entry (plus one byte for the separating NUL the kernel
        # stores) so the total reflects the argv block the spawn actually builds.
        total_bytes += len(arg.encode("utf-8")) + 1
    if total_bytes > MAX_CHILD_ARGV_BYTES:
        raise ValueError(
            f"{parameter} encodes to {total_bytes} bytes, exceeding "
            f"MAX_CHILD_ARGV_BYTES={MAX_CHILD_ARGV_BYTES}; pass bulk data in a file "
            "the script opens rather than on the command line"
        )
