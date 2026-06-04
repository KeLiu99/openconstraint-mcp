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
# Solve runs use the json-stream transport: `--json-stream` emits one JSON object
# per line (each solution carries both the human `default` text and the `json`
# variable values; status and statistics arrive as their own sibling objects),
# `--output-mode json` selects the json section, and `--output-objective` adds
# `_objective` to each solution so the best objective is recoverable. `--statistics`
# is what makes the `{"type":"statistics"}` objects appear at all. Solve-only:
# check (`-c`) and findMUS keep their plain invocation.
_SOLVE_STREAM_ARGS: tuple[str, ...] = (
    "--statistics",
    "--json-stream",
    "--output-mode",
    "json",
    "--output-objective",
)


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


# MiniZinc's `--json-stream` emits one JSON object per line. Each object has a
# `type`; the solve parser consumes `solution`, `status`, `statistics`, `error`,
# and `warning` and ignores every other type, so a future object type can't break
# a solve. Because status and statistics arrive as sibling top-level objects while
# the model's own text is encapsulated inside a solution object's `output` string,
# a model can no longer forge a status verdict or a stat line — the spoofing
# hazard of the old stdout scrape is closed for the solve path.
_STATUS_MAP: dict[str, SolveStatus] = {
    "OPTIMAL_SOLUTION": "optimal",
    "ALL_SOLUTIONS": "satisfied",
    "SATISFIED": "satisfied",
    "UNSATISFIABLE": "unsatisfiable",
    "UNKNOWN": "unknown",
    "UNBOUNDED": "unbounded",
    "UNSAT_OR_UNBOUNDED": "unsat_or_unbounded",
    # A runtime-failure verdict from the driver/solver — e.g. cp-sat rejecting an
    # out-of-range parameter such as a negative or > int32 `random_seed`. Without
    # this entry it falls through `_map_status` to "unknown", silently hiding the
    # failure; map it to "error" so a bad parameter surfaces as an error verdict.
    "ERROR": "error",
}


class _StreamParse(NamedTuple):
    # `status` is the stream's own verdict: an `error` object (seen at any point)
    # forces "error", else the mapped `{"type":"status"}` value, else None —
    # meaning the stream gave no completeness verdict and the caller applies a
    # return-code fallback (a single `satisfy` stops at the first solution and
    # emits no status object).
    status: SolveStatus | None
    solutions: list[dict[str, Any]]
    objective: int | float | None
    statistics: dict[str, str]
    # Reconstructed human text: each solution's `output.default` section, or — when
    # the model has no explicit `output` item, so the stream carries only `json` —
    # a synthesized rendering of that solution's variable map.
    stdout: str
    messages: list[str]  # error/warning diagnostics to surface into `stderr`


def _diagnostic_line(obj: dict[str, Any]) -> str | None:
    """Render an ``error``/``warning`` stream object into one diagnostic line.

    Prefers ``"<what>: <message>"`` (e.g. ``syntax error: unexpected item …``),
    falling back to whichever of ``what``/``message`` is a string. Returns None
    when neither is, so an empty diagnostic is never surfaced.
    """
    what = obj.get("what")
    message = obj.get("message")
    if isinstance(what, str) and isinstance(message, str):
        return f"{what}: {message}"
    if isinstance(message, str):
        return message
    if isinstance(what, str):
        return what
    return None


def _map_status(raw: str, *, has_solution: bool) -> SolveStatus:
    # A known spelling maps directly. An unrecognized verdict (a renamed or newly
    # added MiniZinc status) falls back safely so it never crashes a solve: a
    # solution in hand means "satisfied", otherwise "unknown".
    mapped = _STATUS_MAP.get(raw)
    if mapped is not None:
        return mapped
    return "satisfied" if has_solution else "unknown"


def _render_json_solution(values: dict[str, Any]) -> str:
    # Render a solution's `json` variable map as MiniZinc-style `name = <value>;`
    # lines, one per variable. Used as the human-text fallback when a solution
    # object has no `default` section — i.e. the model has no explicit `output`
    # item, so under `--output-mode json` the stream emits only the `json` section.
    # `_objective` is already stripped by the caller, matching the explicit-output
    # `default` text (which `--output-objective` does not augment).
    return "".join(f"{key} = {json.dumps(value)};\n" for key, value in values.items())


def _reconstruct_stdout(blocks: Sequence[str]) -> str:
    # Rebuild the human solution text from each solution's human block — its
    # `output.default` section, or a `_render_json_solution` rendering when no
    # `default` is present. Each block is made newline-terminated so consecutive
    # solutions stay visually separated; a block already ending in a newline is
    # left as-is (no double blank lines). This restores the "solution text lives in
    # stdout" contract the display path and prompt rely on, now sourced from the
    # stream regardless of whether the model declares an explicit `output` item.
    return "".join(block if block.endswith("\n") else block + "\n" for block in blocks)


def _parse_solve_stream(stdout: str) -> _StreamParse:
    """Parse a ``--json-stream`` solve transcript into structured fields.

    Best-effort and never raises: a line that is not a JSON object (stray text,
    or a half-written final object truncated by a hard timeout) is skipped, and an
    unknown object ``type`` is ignored. ``_objective`` is removed from each
    solution's variable map, and the last solution's value becomes ``objective``
    (None for satisfaction, where no solution carries one).
    """
    solutions: list[dict[str, Any]] = []
    blocks: list[str] = []
    statistics: dict[str, str] = {}
    messages: list[str] = []
    objective: int | float | None = None
    status_raw: str | None = None
    error_seen = False

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # not a JSON object (truncated tail / stray text)
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type")
        if obj_type == "solution":
            output = obj.get("output")
            if not isinstance(output, dict):
                continue
            stripped: dict[str, Any] | None = None
            values = output.get("json")
            if isinstance(values, dict):
                stripped = dict(values)
                obj_val = stripped.pop("_objective", None)
                if isinstance(obj_val, (int, float)) and not isinstance(obj_val, bool):
                    objective = obj_val
                solutions.append(stripped)
            default = output.get("default")
            if isinstance(default, str):
                blocks.append(default)
            elif stripped:
                # No `default` section: the model has no explicit `output` item, so
                # the solution arrives only as the `json` map. Synthesize a human
                # block from it so the reconstructed stdout still shows the solution.
                blocks.append(_render_json_solution(stripped))
        elif obj_type == "status":
            raw = obj.get("status")
            if isinstance(raw, str):
                status_raw = raw
        elif obj_type == "statistics":
            stats = obj.get("statistics")
            if isinstance(stats, dict):
                for key, value in stats.items():
                    statistics[str(key)] = value if isinstance(value, str) else str(value)
        elif obj_type in ("error", "warning"):
            if obj_type == "error":
                error_seen = True
            line_msg = _diagnostic_line(obj)
            if line_msg is not None:
                messages.append(line_msg)
        # any other object type is ignored (forward-compatible)

    if error_seen:
        status: SolveStatus | None = "error"
    elif status_raw is not None:
        status = _map_status(status_raw, has_solution=bool(solutions))
    else:
        status = None
    return _StreamParse(
        status=status,
        solutions=solutions,
        objective=objective,
        statistics=statistics,
        stdout=_reconstruct_stdout(blocks),
        messages=messages,
    )


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


def _merge_stderr(real_stderr: str, messages: Sequence[str]) -> str:
    """Fold json-stream diagnostics into the process's ``stderr`` channel.

    ``--json-stream`` routes model/solver diagnostics into the *stdout* stream as
    ``error``/``warning`` objects, so the real process ``stderr`` is usually
    empty. To keep the "read stderr for diagnostics" contract working regardless
    of channel, the parsed messages are appended here — skipping any the process
    already wrote to ``stderr`` so a double-reported message is not duplicated.
    With no new messages the real ``stderr`` is returned unchanged.
    """
    extra = [m for m in messages if m and m not in real_stderr.splitlines()]
    if not extra:
        return real_stderr
    base = real_stderr if real_stderr == "" or real_stderr.endswith("\n") else real_stderr + "\n"
    return base + "\n".join(extra) + "\n"


def _resolve_status(
    stream_status: SolveStatus | None, *, has_solution: bool, returncode: int
) -> SolveStatus:
    # The stream's own verdict wins when it gave one. Otherwise, apply the
    # return-code fallback: a solution with a clean exit is "satisfied" (the
    # single-`satisfy` case, which emits no status object), a non-zero exit is
    # "error", and an empty clean run is "unknown".
    if stream_status is not None:
        return stream_status
    if has_solution:
        return "satisfied"
    if returncode != 0:
        return "error"
    return "unknown"


def _build_solve_result(outcome: _RunOutcome, *, solver: str) -> SolveResult:
    # Parse the json-stream transcript once; both branches reuse the structured
    # solutions/statistics and the reconstructed human stdout. Diagnostics found
    # in the stream are folded into stderr (the stream routes them off the real
    # stderr channel).
    parsed = _parse_solve_stream(outcome.stdout)
    solution = parsed.solutions[-1] if parsed.solutions else None
    stderr = _merge_stderr(outcome.stderr, parsed.messages)
    if outcome.timed_out:
        # The outer subprocess cap fired: keep whatever the partial stream gave,
        # but the verdict is "timeout" and there is no real return code (expose
        # None, not the internal -1 sentinel).
        return SolveResult(
            status="timeout",
            solver=solver,
            return_code=None,
            timed_out=True,
            stdout=parsed.stdout,
            stderr=stderr,
            elapsed_ms=outcome.elapsed_ms,
            statistics=parsed.statistics,
            solution=solution,
            solutions=parsed.solutions,
            objective=parsed.objective,
        )
    status = _resolve_status(
        parsed.status, has_solution=bool(parsed.solutions), returncode=outcome.returncode
    )
    return SolveResult(
        status=status,
        solver=solver,
        return_code=outcome.returncode,
        timed_out=False,
        stdout=parsed.stdout,
        stderr=stderr,
        elapsed_ms=outcome.elapsed_ms,
        statistics=parsed.statistics,
        solution=solution,
        solutions=parsed.solutions,
        objective=parsed.objective,
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
    # A pure `-c` compile emits no status object, so the process return code is
    # the whole signal here — unlike solve, which classifies from the stream.
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


def _solve_extra_args(
    *,
    free_search: bool,
    parallel: int | None,
    random_seed: int | None,
    all_solutions: bool,
) -> tuple[str, ...]:
    """Build the solve ``extra_args``: the json-stream transport plus any
    optional solver/search-control flags.

    Validates the valued numeric flag (``parallel`` must be ``>= 1``). With
    every flag at its default the result is exactly ``_SOLVE_STREAM_ARGS``, so a
    default solve is byte-identical to the transport-only invocation.
    ``free_search`` -> ``-f``; ``parallel`` -> ``-p N``; ``random_seed`` ->
    ``-r N`` (any int); ``all_solutions`` -> ``-a``. Flags are appended after
    the transport args; MiniZinc is order-insensitive among them.
    """
    if parallel is not None and parallel < 1:
        raise ValueError("parallel must be >= 1")
    flags: list[str] = []
    if free_search:
        flags.append("-f")
    if parallel is not None:
        flags += ["-p", str(parallel)]
    if random_seed is not None:
        flags += ["-r", str(random_seed)]
    if all_solutions:
        flags.append("-a")
    return (*_SOLVE_STREAM_ARGS, *flags)


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    free_search: bool = False,
    parallel: int | None = None,
    random_seed: int | None = None,
    all_solutions: bool = False,
) -> SolveResult:
    outcome = _run_managed_minizinc(
        model,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=_solve_extra_args(
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
        ),
        data=data,
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
    free_search: bool = False,
    parallel: int | None = None,
    random_seed: int | None = None,
    all_solutions: bool = False,
) -> SolveResult:
    """Solve a MiniZinc model read from ``model_path`` via the managed runtime.

    Runs the managed binary on the real ``model_path`` with ``cwd`` = its
    parent, like normal MiniZinc CLI usage, so a relative ``include`` resolves
    against the model's own directory. Returns the inline tool's ``SolveResult``
    shape. The optional solver/search-control flags behave exactly as in
    ``solve_model`` (see ``_solve_extra_args``).
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=_solve_extra_args(
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
        ),
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
