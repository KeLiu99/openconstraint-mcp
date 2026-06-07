from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from ..runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from ..schemas import (
    CheckerReport,
    CheckerStatus,
    CheckResult,
    CheckStatus,
    ModelInspectionResult,
    SolutionCheck,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
)
from .checker import _parse_checker_stream
from .interface import parse_model_interface
from .stream import _parse_solve_stream
from .unsat_core import _parse_unsat_core

DEFAULT_SOLVER: str = "cp-sat"
FINDMUS_SOLVER: str = "org.minizinc.findmus"
# Canonical solver ids whose `.msc` stdFlags include `-n` (verified against the
# managed MiniZinc 2.9.7 runtime). Canonical-only and default-deny: a short alias
# ("gecode") or any unlisted solver — including the default cp-sat — is rejected
# with an actionable error rather than building a doomed `-n` command.
# `org.gecode.gist` is excluded deliberately: it is Gecode's Gist GUI search
# tool, inappropriate for a headless local-first server, even though it lists `-n`.
NUM_SOLUTIONS_SOLVERS: frozenset[str] = frozenset({"org.gecode.gecode", "org.chuffed.chuffed"})
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000
# Named separately from the solve budget so the check call site doesn't read as
# a solve constant. A compile-check is far cheaper than a solve, but reusing the
# same value keeps the two tools' timeout semantics aligned.
DEFAULT_CHECK_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
# Named separately from the solve budget so the findMUS call site describes the
# diagnostic operation even though it uses the same default wall-clock budget.
DEFAULT_UNSAT_CORE_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
# Inspection is a preflight like check — cheaper, even, since it stops after type
# analysis — so it shares the check budget rather than an independent literal that
# could drift. The distinct name keeps the inspect call site from reading as a check.
DEFAULT_INSPECT_TIMEOUT_MS: int = DEFAULT_CHECK_TIMEOUT_MS
# MiniZinc rejects a `--solution-checker` whose filename does not end in `.mzc`
# or `.mzc.mzn` at argument parsing. Matched on the full `name`, NOT `Path.suffix`
# (which returns `.mzn` for `model.mzc.mzn`), so the validator rejects the wrong
# suffix server-side before the doomed run.
_CHECKER_SUFFIXES: tuple[str, ...] = (".mzc", ".mzc.mzn")
# MiniZinc's read-only type-analysis flag: it reports the model's interface
# (required params, output vars, method) and stops without flattening or solving.
# It coexists with the --solver/--time-limit args _build_minizinc_cmd always
# injects, but takes none of the solve transport (--json-stream/--statistics) or
# search-control flags.
INSPECT_FLAG: str = "--model-interface-only"
_MODEL_FILENAME: str = "model.mzn"
_DATA_FILENAME: str = "data.dzn"
_CHECKER_FILENAME: str = "checker.mzc.mzn"
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


def _require_minizinc_binary() -> Path:
    """Return the managed MiniZinc binary, raising if the runtime is absent.

    The runtime-presence gate shared by ``list_solvers`` and both runners, so
    the user-facing "not installed" message lives in one place.
    """
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    return get_minizinc_binary()


def _solver_capabilities(entry: dict[str, Any]) -> SolverCapabilities:
    """Derive a solver's capability facts from one ``--solvers-json`` entry.

    The ``-a/-f/-p/-r`` booleans are honest membership reads of the solver's
    declared ``stdFlags``; ``supports_num_solutions`` is **not** — it follows the
    canonical ``solver_supports_num_solutions`` allowlist, so ``org.gecode.gist``
    stays excluded despite listing ``-n`` and an allowlisted solver stays
    supported even when its ``stdFlags`` is malformed.

    ``stdFlags`` is trusted only when it is a JSON array. An absent key, a
    ``null``, or a scalar string degrades to an empty ``std_flags`` (default-deny
    for the four derived booleans) rather than crashing on ``list(None)`` or
    splitting a string into characters — guarding against runtime-version shape
    drift.
    """
    raw = entry.get("stdFlags")
    std_flags = [str(flag) for flag in raw] if isinstance(raw, list) else []
    return SolverCapabilities(
        supports_all_solutions="-a" in std_flags,
        supports_free_search="-f" in std_flags,
        supports_parallel="-p" in std_flags,
        supports_random_seed="-r" in std_flags,
        supports_num_solutions=solver_supports_num_solutions(str(entry.get("id", ""))),
        std_flags=std_flags,
    )


def list_solvers() -> SolverList:
    binary = _require_minizinc_binary()
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
    solvers: list[SolverInfo] = [
        SolverInfo(
            id=str(solver_id := entry.get("id", "")),
            name=str(entry.get("name", solver_id)),
            version=entry.get("version"),
            tags=list(entry.get("tags", [])),
            capabilities=_solver_capabilities(entry),
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


def _build_minizinc_cmd(
    binary: Path,
    *,
    solver: str,
    timeout_ms: int,
    model_arg: str,
    extra_args: Sequence[str] = (),
    data_args: Sequence[str] = (),
) -> list[str]:
    """Assemble the managed-binary argv shared by both runners.

    Centralizes MiniZinc's positional contract — the model file precedes any
    data file (``model.mzn data.dzn``), with ``--solver``/``--time-limit`` and
    the caller's ``extra_args`` ahead of both — so the two runners can't drift
    on argument order. ``extra_args`` and ``data_args`` default to empty, so a
    minimal (transport-only, dataless) command needs neither.
    """
    return [
        str(binary),
        "--solver",
        solver,
        "--time-limit",
        str(timeout_ms),
        *extra_args,
        model_arg,
        *data_args,
    ]


def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data: str | None = None,
    checker: str | None = None,
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

    When ``checker`` is not ``None`` it is written beside the model as a
    ``.mzc.mzn`` file and added to ``extra_args`` before the command is built, so
    ``_build_minizinc_cmd`` remains the single place that orders flags before
    positional model/data arguments.
    """
    if not model.strip():
        raise ValueError("model must not be empty")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    binary = _require_minizinc_binary()
    with tempfile.TemporaryDirectory(prefix="openconstraint-mcp-") as tmp:
        tmp_dir = Path(tmp)
        model_file = tmp_dir / _MODEL_FILENAME
        model_file.write_text(model, encoding="utf-8")
        data_args: list[str] = []
        if data is not None:
            data_file = tmp_dir / _DATA_FILENAME
            data_file.write_text(data, encoding="utf-8")
            data_args = [str(data_file)]
        effective_extra_args = tuple(extra_args)
        if checker is not None:
            checker_file = tmp_dir / _CHECKER_FILENAME
            checker_file.write_text(checker, encoding="utf-8")
            effective_extra_args = (
                *effective_extra_args,
                "--solution-checker",
                str(checker_file),
            )
        cmd = _build_minizinc_cmd(
            binary,
            solver=solver,
            timeout_ms=timeout_ms,
            extra_args=effective_extra_args,
            model_arg=str(model_file),
            data_args=data_args,
        )
        return _invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(tmp_dir))


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


def _build_inspection_result(outcome: _RunOutcome, *, solver: str) -> ModelInspectionResult:
    """Classify a ``--model-interface-only`` run into a ModelInspectionResult.

    Mirrors ``_build_check_result``'s rc-driven contract — timeout -> ``timeout``,
    a non-zero exit -> ``error`` with the diagnostic on stderr and no parse — then
    on a clean exit parses the single interface object. An unparseable interface on
    rc 0 degrades to ``error`` (with the parse failure folded into stderr) rather
    than mis-reporting a partial interface. ``interface`` is populated only on ``ok``.
    """
    if outcome.timed_out:
        return ModelInspectionResult(
            status="timeout",
            solver=solver,
            interface=None,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    if outcome.returncode != 0:
        return ModelInspectionResult(
            status="error",
            solver=solver,
            interface=None,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    try:
        interface = parse_model_interface(outcome.stdout)
    except ValueError as exc:
        return ModelInspectionResult(
            status="error",
            solver=solver,
            interface=None,
            stdout=outcome.stdout,
            stderr=_merge_stderr(outcome.stderr, [f"interface parse failed: {exc}"]),
            elapsed_ms=outcome.elapsed_ms,
        )
    return ModelInspectionResult(
        status="ok",
        solver=solver,
        interface=interface,
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


def _derive_checker_status(solve: SolveResult, checks: Sequence[SolutionCheck]) -> CheckerStatus:
    """Map a built ``SolveResult`` + parsed checks to the honest aggregate (D4).

    First match wins, so a stream-reported solve error is never hidden behind
    ``no_solution``/``completed``: ``timeout`` (the subprocess cap fired); then
    ``error`` — the solve verdict is ``error`` (a stream ``ERROR`` maps there
    independently of return code), the return code is a nonzero int (broken /
    missing checker, wrong suffix), or the verdict count misaligns with the
    produced solutions (a solution went unchecked); then ``no_solution`` (a clean
    run produced nothing, so no checker ran and both counts are zero); then
    ``violation`` (any per-solution checker rejection — the one verdict the
    server asserts on its own); else ``completed`` (the checker ran for every
    produced solution with no machine-readable violation — NOT "all author-correct").
    """
    if solve.timed_out:
        return "timeout"
    if (
        solve.status == "error"
        or (isinstance(solve.return_code, int) and solve.return_code != 0)
        or len(checks) != len(solve.solutions)
    ):
        return "error"
    if not solve.solutions:
        return "no_solution"
    if any(check.violation for check in checks):
        return "violation"
    return "completed"


def _build_checker_report(outcome: _RunOutcome, solve: SolveResult) -> CheckerReport:
    """Compose the nested checker report for a solve using ``--solution-checker``."""
    checks = _parse_checker_stream(outcome.stdout)
    return CheckerReport(
        status=_derive_checker_status(solve, checks),
        checks=checks,
        transcript=outcome.stdout,
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


def solver_supports_num_solutions(solver: str) -> bool:
    """Return whether ``solver`` accepts the ``-n`` (num-solutions) flag.

    A canonical-id allowlist, default-deny (see ``NUM_SOLUTIONS_SOLVERS``). The
    one invariant shared by the core gate and any future server-side re-run
    decision, so the supported set is defined in a single place.
    """
    return solver in NUM_SOLUTIONS_SOLVERS


def _solve_extra_args(
    *,
    solver: str,
    free_search: bool,
    parallel: int | None,
    random_seed: int | None,
    all_solutions: bool,
    num_solutions: int | None,
) -> tuple[str, ...]:
    """Build the solve ``extra_args``: the json-stream transport plus any
    optional solver/search-control flags.

    Validates the valued numeric flags (``parallel`` must be ``>= 1``;
    ``num_solutions`` must be ``>= 1``). With every flag at its default the
    result is exactly ``_SOLVE_STREAM_ARGS``, so a default solve is
    byte-identical to the transport-only invocation. ``free_search`` -> ``-f``;
    ``parallel`` -> ``-p N``; ``random_seed`` -> ``-r N`` (any int);
    ``all_solutions`` -> ``-a``; ``num_solutions`` -> ``-n N``. Flags are
    appended after the transport args; MiniZinc is order-insensitive among them.

    ``num_solutions`` is solver-gated (satisfaction-only ``-n``): it is appended
    only for a solver in ``NUM_SOLUTIONS_SOLVERS``; for any other solver
    (including the default cp-sat) it raises a ``ValueError`` naming the
    supported solvers, so the doomed ``-n`` command is never built.
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
    if num_solutions is None:
        return *_SOLVE_STREAM_ARGS, *flags
    if num_solutions < 1:
        raise ValueError("num_solutions must be >= 1")
    if not solver_supports_num_solutions(solver):
        raise ValueError(
            f"solver '{solver}' does not support num_solutions (the -n flag). "
            "Retry with solver='org.chuffed.chuffed' or solver='org.gecode.gecode'."
        )
    flags += ["-n", str(num_solutions)]

    return *_SOLVE_STREAM_ARGS, *flags


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    checker: str | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    free_search: bool = False,
    parallel: int | None = None,
    random_seed: int | None = None,
    all_solutions: bool = False,
    num_solutions: int | None = None,
) -> SolveResult:
    outcome = _run_managed_minizinc(
        model,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=_solve_extra_args(
            solver=solver,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        ),
        data=data,
        checker=checker,
    )
    result = _build_solve_result(outcome, solver=solver)
    if checker is not None:
        result.checker = _build_checker_report(outcome, result)
    return result


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


def inspect_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
) -> ModelInspectionResult:
    """Inspect a MiniZinc model's interface WITHOUT solving it.

    Wraps the managed runtime's ``--model-interface-only`` flag: it reports which
    parameters the model still needs as data (``required_parameters``), its output
    variables, their types, and the solve method, stopping after type analysis —
    no flattening, no search. With no ``data`` the required set is the model's full
    parameter list; supplying the matching ``data`` shrinks it, so an empty
    ``required_parameters`` signals the data is complete. ``status="ok"`` means
    only that the interface was extracted — NOT that the data is complete.
    """
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=(INSPECT_FLAG,), data=data
    )
    return _build_inspection_result(outcome, solver=solver)


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
    if data_path is None:
        return model_path, None
    data_path = data_path.resolve()
    if not data_path.exists():
        raise ValueError(f"data_path does not exist: {data_path}")
    if not data_path.is_file():
        raise ValueError(f"data_path is not a file: {data_path}")
    return model_path, data_path


def _validate_checker_path(checker_path: Path) -> Path:
    """Resolve, validate, and return the checker path before any subprocess.

    A sibling of ``_validate_model_data_paths`` for the ``--solution-checker``
    argument: resolves to absolute (so the flag is unambiguous regardless of
    cwd), then rejects — with a clear ``ValueError`` naming the path — a checker
    whose filename does not end in ``.mzc``/``.mzc.mzn`` (MiniZinc rejects other
    suffixes at argument parsing; the check is on ``name``, not ``Path.suffix``,
    which returns ``.mzn`` for ``model.mzc.mzn``), a missing or non-regular-file
    checker, and a non-UTF-8 checker. The resolved absolute path is returned so
    the caller uses the same path the validation ran against.
    """
    checker_path = checker_path.resolve()
    if not checker_path.name.endswith(_CHECKER_SUFFIXES):
        raise ValueError(f"checker_path must end in .mzc or .mzc.mzn: {checker_path}")
    if not checker_path.exists():
        raise ValueError(f"checker_path does not exist: {checker_path}")
    if not checker_path.is_file():
        raise ValueError(f"checker_path is not a file: {checker_path}")
    _read_text_utf8(checker_path)  # reject non-UTF-8 with a clear ValueError
    return checker_path


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
    binary = _require_minizinc_binary()
    data_args = [str(data_path)] if data_path is not None else []
    cmd = _build_minizinc_cmd(
        binary,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        model_arg=str(model_path),
        data_args=data_args,
    )
    return _invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(model_path.parent))


def solve_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    checker_path: Path | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    free_search: bool = False,
    parallel: int | None = None,
    random_seed: int | None = None,
    all_solutions: bool = False,
    num_solutions: int | None = None,
) -> SolveResult:
    """Solve a MiniZinc model read from ``model_path`` via the managed runtime.

    Runs the managed binary on the real ``model_path`` with ``cwd`` = its
    parent, like normal MiniZinc CLI usage, so a relative ``include`` resolves
    against the model's own directory. Returns the inline tool's ``SolveResult``
    shape. The optional solver/search-control flags behave exactly as in
    ``solve_model`` (see ``_solve_extra_args``). When ``checker_path`` is
    supplied, it is validated as a ``.mzc``/``.mzc.mzn`` checker and added to the
    same solve invocation; the returned ``SolveResult.checker`` then carries the
    per-solution checker report.
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    checker_path = _validate_checker_path(checker_path) if checker_path is not None else None
    extra_args = _solve_extra_args(
        solver=solver,
        free_search=free_search,
        parallel=parallel,
        random_seed=random_seed,
        all_solutions=all_solutions,
        num_solutions=num_solutions,
    )
    if checker_path is not None:
        extra_args = (*extra_args, "--solution-checker", str(checker_path))
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        data_path=data_path,
    )
    result = _build_solve_result(outcome, solver=solver)
    if checker_path is not None:
        result.checker = _build_checker_report(outcome, result)
    return result


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


def inspect_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
) -> ModelInspectionResult:
    """Inspect a MiniZinc model read from ``model_path`` via the managed runtime.

    Same CLI-style execution as ``solve_model_path`` (real path, ``cwd`` = the
    model's parent), so a relative ``include`` resolves against the model's own
    directory; returns the inline ``inspect_model`` ``ModelInspectionResult`` shape.
    """
    model_path, data_path = _validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=(INSPECT_FLAG,),
        data_path=data_path,
    )
    return _build_inspection_result(outcome, solver=solver)


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
