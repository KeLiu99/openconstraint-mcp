from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from .runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from .schemas import (
    CheckResult,
    CheckStatus,
    SolveResult,
    SolverInfo,
    SolverList,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
)

DEFAULT_SOLVER: str = "cp-sat"
FINDMUS_SOLVER: str = "org.minizinc.findmus"
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000
# Named separately from the solve budget so the check call site doesn't read as
# a solve constant. A compile-check is far cheaper than a solve, but reusing the
# same value keeps the two tools' timeout semantics aligned.
DEFAULT_CHECK_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
# Named separately from the solve budget so the findMUS call site describes the
# diagnostic operation even though it uses the same default wall-clock budget.
DEFAULT_UNSAT_CORE_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
_MODEL_FILENAME: str = "model.mzn"
_DATA_FILENAME: str = "data.dzn"


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


def _parse_status(stdout: str, returncode: int, timed_out: bool) -> SolveStatus:
    if timed_out:
        return "timeout"
    # FlatZinc status markers always occupy their own line, so match whole
    # stripped lines rather than substrings — otherwise a model whose output
    # block prints a rule of dashes/equals or the literal marker text would be
    # misclassified.
    lines = {line.strip() for line in stdout.splitlines()}
    if "=====ERROR=====" in lines:
        return "error"
    if "=====UNSATISFIABLE=====" in lines:
        return "unsatisfiable"
    if "=====UNBOUNDED=====" in lines:
        return "unbounded"
    if "=====UNSATorUNBOUNDED=====" in lines:
        return "unsat_or_unbounded"
    if "=====UNKNOWN=====" in lines:
        return "unknown"
    if "==========" in lines:
        return "optimal"
    if "----------" in lines:
        return "satisfied"
    if returncode != 0:
        return "error"
    return "unknown"


def _coerce_to_text(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def list_solvers() -> SolverList:
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    try:
        completed = subprocess.run(
            [str(binary), "--solvers-json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = stderr or str(exc)
        raise MiniZincExecutionError(
            f"Managed MiniZinc binary at {binary} failed to list solvers: {detail}. "
            "The runtime may be corrupt — try reinstalling with "
            "`openconstraint-mcp install-runtime`."
        ) from exc
    raw: list[dict[str, Any]] = json.loads(completed.stdout)
    solvers = [
        SolverInfo(
            id=str(entry.get("id", "")),
            name=str(entry.get("name", entry.get("id", ""))),
            version=entry.get("version"),
            tags=list(entry.get("tags", [])),
        )
        for entry in raw
    ]
    return SolverList(solvers=solvers)


class _RunOutcome(NamedTuple):
    timed_out: bool
    returncode: int  # meaningful only when timed_out is False
    stdout: str
    stderr: str
    elapsed_ms: int


def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data: str | None = None,
) -> _RunOutcome:
    """Run the managed MiniZinc binary on ``model`` and report the raw outcome.

    Shared by ``solve_model`` (``extra_args=()``) and ``check_model``
    (``extra_args=("-c",)``). The model is written into a private temp dir and
    ``subprocess.run`` is pinned to that dir via ``cwd`` so cwd-relative
    ``include`` statements resolve onto emptiness rather than the server's
    working directory. With no data the model file is the last command
    argument.

    When ``data`` is not ``None`` it is written verbatim to a sibling
    ``data.dzn`` in the same temp dir and appended as a positional argument
    *after* the model file — MiniZinc's documented ``<model>.mzn <data>.dzn``
    order, which (unlike the ``--data`` flag) every solver path accepts,
    including the findMUS meta-solver. ``None`` means no data file and no extra
    argument — byte-identical to a dataless run. An empty string is a valid
    "no parameters" input, so it is *not* rejected.
    """
    if not model.strip():
        raise ValueError("model must not be empty")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    subprocess_timeout = (timeout_ms / 1000) + 5
    with tempfile.TemporaryDirectory(prefix="openconstraint-mcp-") as tmp:
        tmp_dir = Path(tmp)
        model_file = tmp_dir / _MODEL_FILENAME
        model_file.write_text(model, encoding="utf-8")
        data_args: list[str] = []
        if data is not None:
            data_file = tmp_dir / _DATA_FILENAME
            data_file.write_text(data, encoding="utf-8")
            data_args = [str(data_file)]
        cmd = [
            str(binary),
            "--solver",
            solver,
            "--time-limit",
            str(timeout_ms),
            *extra_args,
            str(model_file),
            *data_args,
        ]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=subprocess_timeout,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
            return _RunOutcome(
                timed_out=True,
                returncode=-1,  # sentinel: never read while timed_out is True
                stdout=_coerce_to_text(exc.stdout),
                stderr=_coerce_to_text(exc.stderr),
                elapsed_ms=elapsed_ms,
            )
        except OSError as exc:
            raise MiniZincExecutionError(
                f"Managed MiniZinc binary at {binary} failed to execute: {exc}. "
                "The runtime may be corrupt — try reinstalling with "
                "`openconstraint-mcp install-runtime`."
            ) from exc
        elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
        return _RunOutcome(
            timed_out=False,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_ms=elapsed_ms,
        )


def _slice_source(model: str, sl: int, sc: int, el: int, ec: int) -> str:
    lines = model.splitlines()
    # Reject an unusable line span: before the file, inverted, or starting past EOF.
    if sl < 1 or el < 1 or sl > el or sl > len(lines):
        return ""

    # Clamp the end line into the file; the guard above keeps this non-empty.
    referenced = lines[sl - 1 : min(el, len(lines))]
    fallback = "\n".join(referenced)
    first, last = referenced[0], referenced[-1]

    # Slice precisely only when every column bound lands inside its line (and, on
    # a single line, start precedes end). An end line past EOF means the end
    # column can't be trusted, so fall back to the whole referenced line(s).
    cols_usable = (
        el <= len(lines)
        and 1 <= sc <= len(first)
        and 1 <= ec <= len(last)
        and (sl != el or sc <= ec)
    )
    if not cols_usable:
        return fallback

    if sl == el:
        return first[sc - 1 : ec]
    return "\n".join([first[sc - 1 :], *referenced[1:-1], last[:ec]])


# findMUS prints each MUS constraint as a pipe-delimited trace span:
# <file>|<start-line>|<start-col>|<end-line>|<end-col>|... — capture the file
# token (ending in .mzn) and the four 1-indexed coordinates.
_SPAN_PATTERN = re.compile(r"([^\s|;]+\.mzn)\|(\d+)\|(\d+)\|(\d+)\|(\d+)")


def _parse_unsat_core(stdout: str, model: str) -> tuple[bool, list[UnsatCoreConstraint]]:
    mus_present = any(line.lstrip().startswith("MUS:") for line in stdout.splitlines())
    if not mus_present:
        return False, []

    # Keyed by span so repeated trace lines for the same constraint collapse to a
    # single entry, with first-seen order preserved (dict keeps insertion order).
    by_span: dict[tuple[int, int, int, int], UnsatCoreConstraint] = {}
    for file_name, sl_raw, sc_raw, el_raw, ec_raw in _SPAN_PATTERN.findall(stdout):
        if Path(file_name).name != _MODEL_FILENAME:
            continue
        sl, sc, el, ec = int(sl_raw), int(sc_raw), int(el_raw), int(ec_raw)
        span = (sl, sc, el, ec)
        if span not in by_span:
            by_span[span] = UnsatCoreConstraint(
                line=sl,
                column=sc,
                end_line=el,
                end_column=ec,
                source=_slice_source(model, sl, sc, el, ec),
            )

    return True, list(by_span.values())


def find_unsat_core(
    model: str,
    *,
    data: str | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
) -> UnsatCoreResult:
    outcome = _run_managed_minizinc(
        model,
        solver=FINDMUS_SOLVER,
        timeout_ms=timeout_ms,
        extra_args=(),
        data=data,
    )
    if outcome.timed_out:
        return UnsatCoreResult(
            status="timeout",
            core=[],
            message="findMUS timed out before reporting a result.",
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    mus_present, core = _parse_unsat_core(outcome.stdout, model)
    if mus_present:
        if core:
            message = (
                "findMUS reported a minimal unsatisfiable subset; "
                f"{len(core)} constraint location(s) resolved from the submitted model."
            )
        else:
            message = (
                "findMUS reported a minimal unsatisfiable subset, but no submitted-model "
                "constraint locations were resolved; see stdout."
            )
        return UnsatCoreResult(
            status="mus_found",
            core=core,
            message=message,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    if outcome.returncode != 0:
        return UnsatCoreResult(
            status="error",
            core=[],
            message="findMUS did not complete successfully; see stderr.",
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    return UnsatCoreResult(
        status="no_core",
        core=[],
        message="findMUS completed without reporting a minimal unsatisfiable subset.",
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult:
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=(), data=data
    )
    if outcome.timed_out:
        return SolveResult(
            status="timeout",
            solver=solver,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    status = _parse_status(outcome.stdout, outcome.returncode, timed_out=False)
    return SolveResult(
        status=status,
        solver=solver,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def check_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult:
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=("-c",), data=data
    )
    if outcome.timed_out:
        return CheckResult(
            status="timeout",
            solver=solver,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    # A pure `-c` compile emits no FlatZinc status markers, so the process
    # return code is the whole signal here — unlike solve_model, do not route
    # this through _parse_status.
    status: CheckStatus = "ok" if outcome.returncode == 0 else "error"
    return CheckResult(
        status=status,
        solver=solver,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )
