from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
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


def _format_unsat_core_message(core: Sequence[UnsatCoreConstraint]) -> str:
    if core:
        return (
            "findMUS reported a minimal unsatisfiable subset; "
            f"{len(core)} constraint location(s) resolved from the submitted model."
        )
    return (
        "findMUS reported a minimal unsatisfiable subset, but no submitted-model "
        "constraint locations were resolved; see stdout."
    )


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


def _invoke_minizinc(cmd: Sequence[str], *, timeout_ms: int, cwd: str) -> _RunOutcome:
    """Run a fully-built MiniZinc ``cmd`` and capture the raw outcome.

    Shared by the inline runner (``cwd`` is a private temp dir) and the
    path-based runner (``cwd`` is the model's own directory). The
    subprocess gets a wall-clock cap of ``(timeout_ms / 1000) + 5`` seconds —
    a few seconds past MiniZinc's own ``--time-limit`` so the binary normally
    stops itself first, and we capture its partial output. A ``TimeoutExpired``
    becomes a ``timed_out`` outcome; an ``OSError`` (binary missing/not
    executable) is wrapped as ``MiniZincExecutionError`` keyed on ``cmd[0]``.
    """
    subprocess_timeout = (timeout_ms / 1000) + 5
    start = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=subprocess_timeout,
            cwd=cwd,
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
            f"Managed MiniZinc binary at {cmd[0]} failed to execute: {exc}. "
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
        return _invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(tmp_dir))


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


def _iter_model_spans(stdout: str, model_filename: str) -> Iterator[tuple[int, int, int, int]]:
    # Keep only spans whose file token matches the entry model's basename;
    # spans from included files stay out of the structured core. The match is
    # basename-only, so an included file sharing the entry model's basename in
    # a different directory would be mis-attributed here — a documented
    # best-effort limitation (raw stdout stays authoritative).
    for file_name, sl_raw, sc_raw, el_raw, ec_raw in _SPAN_PATTERN.findall(stdout):
        if Path(file_name).name == model_filename:
            yield int(sl_raw), int(sc_raw), int(el_raw), int(ec_raw)


def _constraint_from_span(model: str, span: tuple[int, int, int, int]) -> UnsatCoreConstraint:
    sl, sc, el, ec = span
    return UnsatCoreConstraint(
        line=sl,
        column=sc,
        end_line=el,
        end_column=ec,
        source=_slice_source(model, sl, sc, el, ec),
    )


def _parse_unsat_core(
    stdout: str, model: str, *, model_filename: str = _MODEL_FILENAME
) -> tuple[bool, list[UnsatCoreConstraint]]:
    mus_present = any(line.lstrip().startswith("MUS:") for line in stdout.splitlines())
    if not mus_present:
        return False, []

    # Repeated trace lines for the same constraint collapse to a single entry,
    # with first-seen order preserved (dict keeps insertion order).
    unique_spans = dict.fromkeys(_iter_model_spans(stdout, model_filename))
    return True, [_constraint_from_span(model, span) for span in unique_spans]


def _build_solve_result(outcome: _RunOutcome, *, solver: str) -> SolveResult:
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


def _build_check_result(outcome: _RunOutcome, *, solver: str) -> CheckResult:
    if outcome.timed_out:
        return CheckResult(
            status="timeout",
            solver=solver,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    # A pure `-c` compile emits no FlatZinc status markers, so the process
    # return code is the whole signal here — unlike solve, do not route this
    # through _parse_status.
    status: CheckStatus = "ok" if outcome.returncode == 0 else "error"
    return CheckResult(
        status=status,
        solver=solver,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def _build_unsat_core_result(
    outcome: _RunOutcome, model: str, *, model_filename: str = _MODEL_FILENAME
) -> UnsatCoreResult:
    if outcome.timed_out:
        return UnsatCoreResult(
            status="timeout",
            core=[],
            message="findMUS timed out before reporting a result.",
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    mus_present, core = _parse_unsat_core(outcome.stdout, model, model_filename=model_filename)
    if mus_present:
        return UnsatCoreResult(
            status="mus_found",
            core=core,
            message=_format_unsat_core_message(core),
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
    return _build_unsat_core_result(outcome, model)


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
    return _build_solve_result(outcome, solver=solver)


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
    return _build_check_result(outcome, solver=solver)


def _read_text_utf8(path: Path) -> str:
    """Read ``path`` as UTF-8, surfacing a bad encoding as a clear ValueError.

    The path tools assume UTF-8 source (MiniZinc's convention); wrapping
    ``UnicodeDecodeError`` here turns an opaque traceback into the repo's
    "clear errors" bar, with the offending path named in the message.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8") from exc


def _validate_model_data_paths(
    model_path: Path, data_path: Path | None
) -> tuple[Path, Path | None]:
    """Resolve, validate, and return the model/data paths before any subprocess.

    Resolves each input to an absolute path (``Path.resolve()`` — following a
    symlink the caller named), then rejects a missing or non-regular-file
    model/data, and an empty/whitespace-only or non-UTF-8 model, with a clear
    ``ValueError`` naming the offending path. The resolved paths are *returned*
    so callers use the same path for read, argv, and cwd — a relative input
    can't then double-count its subdir (``cwd=parent`` + relative argv).

    Model emptiness and UTF-8 are checked here (the model is read for the
    check), so the failure is a clear ``ValueError`` before any run. Data
    emptiness is allowed (a valid "no parameters" input, matching the inline
    ``data`` contract).
    """
    model_path = model_path.resolve()
    if not model_path.exists():
        raise ValueError(f"model_path does not exist: {model_path}")
    if not model_path.is_file():
        raise ValueError(f"model_path is not a file: {model_path}")
    if not _read_text_utf8(model_path).strip():
        raise ValueError(f"model file is empty: {model_path}")
    if data_path is not None:
        data_path = data_path.resolve()
        if not data_path.exists():
            raise ValueError(f"data_path does not exist: {data_path}")
        if not data_path.is_file():
            raise ValueError(f"data_path is not a file: {data_path}")
    return model_path, data_path


def _run_managed_minizinc_paths(
    model_path: Path,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data_path: Path | None = None,
) -> _RunOutcome:
    """Run the managed binary on the real ``model_path`` with ``cwd`` = its parent.

    The path-based counterpart of ``_run_managed_minizinc``: instead of copying
    the model into a private temp dir, it runs the resolved real path so
    relative ``include`` statements resolve against the model's own directory,
    like normal MiniZinc CLI usage. Receives the already-resolved absolute paths
    from the public functions. The data file, when present, is appended
    positionally after the model (MiniZinc's ``model.mzn data.dzn`` order, which
    every solver path — including findMUS — accepts).
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    binary = get_minizinc_binary()
    data_args = [str(data_path)] if data_path is not None else []
    cmd = [
        str(binary),
        "--solver",
        solver,
        "--time-limit",
        str(timeout_ms),
        *extra_args,
        str(model_path),
        *data_args,
    ]
    return _invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(model_path.parent))


def solve_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult:
    """Solve a MiniZinc model read from ``model_path`` via the managed runtime.

    Runs the managed binary on the real ``model_path`` with ``cwd`` = its
    parent, like normal MiniZinc CLI usage, so a relative ``include`` resolves
    against the model's own directory. Returns the inline tool's ``SolveResult``
    shape.
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=(),
        data_path=data_path,
    )
    return _build_solve_result(outcome, solver=solver)


def check_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult:
    """Compile-check a MiniZinc model read from ``model_path`` via the runtime.

    Same CLI-style execution as ``solve_model_path`` (real path, ``cwd`` = the
    model's parent); returns the inline ``check_model`` ``CheckResult`` shape.
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=("-c",),
        data_path=data_path,
    )
    return _build_check_result(outcome, solver=solver)


def find_unsat_core_path(
    model_path: Path,
    *,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
) -> UnsatCoreResult:
    """Compute a MUS for a model read from ``model_path`` via the runtime.

    Same CLI-style execution as ``solve_model_path``. The model runs under its
    real basename, so the structured ``core`` is filtered by ``model_path.name``
    (best-effort, basename-only — see ``_iter_model_spans``); raw ``stdout``
    stays authoritative.
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=FINDMUS_SOLVER,
        timeout_ms=timeout_ms,
        extra_args=(),
        data_path=data_path,
    )
    model = _read_text_utf8(model_path)
    return _build_unsat_core_result(outcome, model, model_filename=model_path.name)
