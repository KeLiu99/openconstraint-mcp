"""Subprocess executor for OR-Tools CP-SAT Python scripts.

The server executes user/LLM-provided Python in a child process using the
server's own interpreter (``sys.executable``), which ships ``ortools``.

Security posture: timeout + output cap + process-tree kill is a **robustness**
boundary, not a security sandbox. No network blocking, AST filtering, or
syscall restriction is applied. This is a local-only tool; a cloud deployment
would require a real sandbox.

Output contract (executor ↔ script): the script must print, as its **last**
stdout block, one JSON object:
    {"status": "<CpsatStatus value>", "objective": <number|null>, "solution": {...},
     "best_objective_bound": <number|null>}

``best_objective_bound`` is optional (a script predating it is parsed as
``None``) and diagnostic only — it is OR-Tools' ``solver.best_objective_bound``,
not a proven objective, and is never consulted for acceptance or winner
selection. It is most useful on ``status="unknown"``, where ``objective`` is
``None`` but the solver may still have made bound progress.

FEASIBILITY-PROBLEM PITFALL: for a pure satisfaction model with no
``model.minimize``/``maximize`` call, ``model.has_objective()`` is ``False``,
and OR-Tools does NOT raise for that case — both ``solver.objective_value``
and ``solver.best_objective_bound`` silently return ``0.0``. A script that
omits the ``if model.has_objective() else None`` guard would report a
meaningless ``best_objective_bound: 0.0`` instead of ``null`` for a
feasibility problem. The executor cannot detect or correct this server-side
(it only parses whatever number the script prints) — the guard must live in
the script itself, which is why the canonical snippet and the
``solve_cpsat_python`` prompt both apply it.

The executor parses the last JSON object it finds in stdout and maps the
``status`` field to ``CpsatStatus``; any unrecognized value becomes ``"error"``.

The child runs unbuffered (``python -u``), so a script MAY print intermediate
result blocks of the same shape during search (e.g. one per improved solution
from a ``CpSolverSolutionCallback``). On a clean exit the final block wins as
usual; on a timeout the executor recovers the last intermediate block's
``solution``/``objective``/``best_objective_bound`` (status stays ``"timeout"``
— a partial is unproven).

Canonical emit snippet (inlined in scripts, never imported from here):

    import json
    status_map = {
        "OPTIMAL": "optimal",
        "FEASIBLE": "feasible",
        "INFEASIBLE": "infeasible",
        "UNKNOWN": "unknown",
        "MODEL_INVALID": "error",
    }
    print(json.dumps({
        "status": status_map.get(solver.status_name(status), "error"),
        "objective": solver.objective_value if model.has_objective() else None,
        "solution": {v.name: solver.value(v) for v in variables},
        "best_objective_bound": solver.best_objective_bound if model.has_objective() else None,
    }))
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Callable
from pathlib import Path
from subprocess import Popen
from typing import Any

from ..schemas.cpsat import CpsatPythonResult, CpsatStatus
from ..shared.childproc import ChildProcessTracker
from ..shared.save_target import text_sha256
from .diagnostics import cpsat_result_diagnostic
from .runner import ChildExecutionResult, execute_child
from .runner import python_script_argv as _python_script_argv

DEFAULT_PYEXEC_TIMEOUT_MS: int = 30_000

# Environment variable the seeded save replay sets for the child, carrying the
# replay CP-SAT random seed. The client-generated script must read it and assign
# ``solver.parameters.random_seed``; the server cannot force a seed into arbitrary
# Python.
CPSAT_SEED_ENV_VAR: str = "OPENCONSTRAINT_MCP_CPSAT_SEED"

# OR-Tools CP-SAT's random_seed parameter is a signed int32. Reject values outside
# that range before they reach a child process.
CPSAT_RANDOM_SEED_MIN: int = -2_147_483_648
CPSAT_RANDOM_SEED_MAX: int = 2_147_483_647

# Environment variable an experiment attempt (or a config-carrying save replay)
# sets for the child, carrying the path to a temporary JSON config file. A
# cooperating script reads it and applies whichever fields it understands; the
# server never sets OR-Tools parameters itself — see CpsatPythonExperimentAttempt.
CPSAT_CONFIG_ENV_VAR: str = "OPENCONSTRAINT_MCP_CPSAT_CONFIG"

VERIFIED_STATUSES: frozenset[CpsatStatus] = frozenset({"optimal", "feasible"})

# Statuses a script may legitimately report. "timeout" is executor-determined, so a
# script claiming it is treated as a contract violation and normalized to "error".
_SCRIPT_STATUSES: frozenset[str] = frozenset(
    {"optimal", "feasible", "infeasible", "unknown", "error"}
)


def validate_checker_args(*, checker: str | None, checker_timeout_ms: int | None) -> None:
    """Validate shared optional-checker arguments for CP-SAT Python tools."""
    if checker_timeout_ms is not None and checker is None:
        raise ValueError("checker_timeout_ms supplied without checker: no checker will run")
    if checker_timeout_ms is not None and checker_timeout_ms <= 0:
        raise ValueError("checker_timeout_ms must be positive")
    if checker is not None and not checker.strip():
        raise ValueError("checker must be non-empty after stripping whitespace")


def effective_checker_timeout_ms(*, checker_timeout_ms: int | None, default_timeout_ms: int) -> int:
    """Return the checker timeout after applying the tool's default timeout fallback."""
    return checker_timeout_ms if checker_timeout_ms is not None else default_timeout_ms


def validate_cpsat_random_seed(seed: object, *, label: str = "seed") -> int:
    """Validate a seed for OR-Tools CP-SAT's ``random_seed`` parameter."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(
            f"{label} must be a non-bool integer in the CP-SAT random_seed range "
            f"{CPSAT_RANDOM_SEED_MIN}..{CPSAT_RANDOM_SEED_MAX}, got {seed!r}"
        )
    if not (CPSAT_RANDOM_SEED_MIN <= seed <= CPSAT_RANDOM_SEED_MAX):
        raise ValueError(
            f"{label} must be in the CP-SAT random_seed range "
            f"{CPSAT_RANDOM_SEED_MIN}..{CPSAT_RANDOM_SEED_MAX}, got {seed!r}"
        )
    return seed


def _canonical_json_dumps(value: dict[str, Any]) -> str:
    """Serialize value with sorted keys and no extra whitespace.

    The single definition of "canonical" for CP-SAT config hashing, so
    ``canonical_json_sha256`` and ``canonical_json_byte_length`` can never
    disagree about what they are hashing/measuring.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_json_sha256(value: dict[str, Any]) -> str:
    """Return the sha256 hex digest of value's canonical JSON serialization.

    Sorted keys mean two dicts with the same keys in different insertion order
    hash identically. Shared by the experiment executor (execution-time config
    hash) and the save gate (save-time mismatch check) so the two can never
    drift apart — see ``config_sha256`` for the "no config" normalization atop
    this.
    """
    return text_sha256(_canonical_json_dumps(value))


def canonical_json_byte_length(value: dict[str, Any]) -> int:
    """Return the byte length of value's canonical JSON encoding (for size bounds)."""
    return len(_canonical_json_dumps(value).encode("utf-8"))


def config_sha256(config: dict[str, Any] | None) -> str | None:
    """Return config's canonical hash, or ``None`` for the "no config" state.

    An empty dict (``{}``) and an omitted config (``None``) both mean "no
    config" — no temp file, no env var, and this returns ``None`` for both, so
    hashes and the save replay gate never have to distinguish ``{}`` from absent.
    """
    if not config:
        return None
    return canonical_json_sha256(config)


def write_config_file(directory: Path, config: dict[str, Any]) -> Path:
    """Write config as JSON into directory and return the file path.

    The caller supplies directory — typically a per-attempt or per-save-run
    ``tempfile.TemporaryDirectory()`` — so the file's lifetime is scoped to that
    context and cleaned up on every exit path, including a timeout tree-kill.
    """
    path = directory / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def seed_config_env(*, seed: int | None, config_path: Path | None) -> dict[str, str | None]:
    """Build the child env overlay for an optional seed and/or config file path.

    Always returns both protocol keys — set to the requested value, or
    explicitly ``None`` when not requested. ``execute_child`` treats a ``None``
    value as "delete this key from the inherited environment", so an
    attempt/replay documented as seed=None/config=None actually clears any
    ``OPENCONSTRAINT_MCP_CPSAT_SEED``/``_CONFIG`` the *parent* (server) process
    happens to have inherited from its own launch environment, instead of
    silently letting a stale value leak into the child.
    """
    return {
        CPSAT_SEED_ENV_VAR: str(seed) if seed is not None else None,
        CPSAT_CONFIG_ENV_VAR: str(config_path) if config_path is not None else None,
    }


def normalize_status(raw: object) -> CpsatStatus:
    if isinstance(raw, str) and raw in _SCRIPT_STATUSES:
        return raw  # type: ignore[return-value]
    return "error"


def normalize_objective(raw: object) -> float | int | None:
    """Accept only a finite real number; bool, non-numeric, and non-finite become None.

    ``int`` is always mathematically finite, so it is returned as-is — including
    values too large to fit a float, which would overflow ``math.isfinite``. The
    finiteness check applies only to ``float`` (rejecting ``nan``/``inf``).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    return raw


def parse_last_json(text: str) -> dict | None:
    """Return the last top-level JSON object found in ``text``, or ``None``.

    Scans forward, decoding each top-level object with ``raw_decode`` so trailing
    output after the final JSON block (a stray log line, a late callback) does not
    defeat parsing, and so a nested object (e.g. ``solution``) inside the payload
    is never mistaken for the result. The last object that decodes wins.
    """
    decoder = json.JSONDecoder()
    found: dict | None = None
    index = text.find("{")
    while index >= 0:
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        index = text.find("{", end)
    return found


def _extract_solution_objective(
    parsed: dict,
) -> tuple[dict | None, float | int | None, float | int | None]:
    """Pull the solution dict, objective, and best_objective_bound out of a result block.

    One site for the shape rules so the clean-exit and timeout (partial-recovery)
    paths can never drift: ``solution`` must be a dict, ``objective`` and
    ``best_objective_bound`` each a real number (same normalization rule for both —
    a diagnostic bound is just as invalid as a bad objective if it isn't finite/numeric).
    """
    solution = parsed.get("solution") if isinstance(parsed.get("solution"), dict) else None
    objective = normalize_objective(parsed.get("objective"))
    best_objective_bound = normalize_objective(parsed.get("best_objective_bound"))
    return solution, objective, best_objective_bound


def _result_from_child(child: ChildExecutionResult) -> CpsatPythonResult:
    """Parse a raw ``ChildExecutionResult`` into the CP-SAT result contract.

    This is the CP-SAT protocol layer: the generic ``execute_child`` knows nothing
    about ``status``/``objective``/``solution``; that parsing lives here so the
    clean-exit, timeout, and truncation shapes are decided in one place. The
    structured diagnostic is derived from the finished result as the single tail.
    """
    result = _classify_child_result(child)
    result.diagnostic = cpsat_result_diagnostic(result)
    return result


def _classify_child_result(child: ChildExecutionResult) -> CpsatPythonResult:
    if child.timed_out:
        # Recover the best-so-far if the script emitted intermediate result blocks
        # (e.g. one per improved solution from a CpSolverSolutionCallback). The
        # last block wins; the unbuffered child (-u) is what lets it survive the
        # kill. Status stays the executor-owned "timeout" — a partial is unproven,
        # never "optimal".
        partial = parse_last_json(child.stdout)
        solution, objective, best_objective_bound = (
            _extract_solution_objective(partial) if partial is not None else (None, None, None)
        )
        return CpsatPythonResult(
            status="timeout",
            solution=solution,
            objective=objective,
            best_objective_bound=best_objective_bound,
            stdout=child.stdout,
            stderr=child.stderr,
            # The child was killed; its exit code (SIGTERM -> -15 on POSIX) is not a
            # real return code. Report null by contract — matching the MiniZinc-path
            # tools — so clients don't misread a timeout as a child error.
            return_code=None,
            timed_out=True,
            truncated=child.truncated,
            duration_ms=child.duration_ms,
        )

    if child.truncated:
        return CpsatPythonResult(
            status="error",
            solution=None,
            objective=None,
            stdout=child.stdout,
            stderr=child.stderr,
            return_code=child.return_code,
            timed_out=False,
            truncated=True,
            duration_ms=child.duration_ms,
        )

    parsed = parse_last_json(child.stdout)
    if parsed is None or child.return_code != 0:
        return CpsatPythonResult(
            status="error",
            solution=None,
            objective=None,
            stdout=child.stdout,
            stderr=child.stderr,
            return_code=child.return_code,
            timed_out=False,
            truncated=False,
            duration_ms=child.duration_ms,
        )

    status = normalize_status(parsed.get("status"))
    solution, objective, best_objective_bound = _extract_solution_objective(parsed)
    return CpsatPythonResult(
        status=status,
        solution=solution,
        objective=objective,
        best_objective_bound=best_objective_bound,
        stdout=child.stdout,
        stderr=child.stderr,
        return_code=child.return_code,
        timed_out=False,
        truncated=False,
        duration_ms=child.duration_ms,
    )


def _validate_script_path(script_path: Path) -> Path:
    """Resolve and validate a CP-SAT Python script path before any subprocess.

    Mirrors the MiniZinc path tools' contract (``_validate_model_data_paths``):
    resolve to an absolute path (following a symlink the caller named), then
    reject a missing or non-regular file, and an empty/whitespace-only or
    non-UTF-8 script, with a clear ``ValueError`` naming the offending path. The
    resolved path is returned so the caller uses the same path for argv and its
    parent for ``cwd`` — a relative input can't then double-count its subdir.
    """
    script_path = script_path.resolve()
    if not script_path.exists():
        raise ValueError(f"script_path does not exist: {script_path}")
    if not script_path.is_file():
        raise ValueError(f"script_path is not a file: {script_path}")
    try:
        text = script_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{script_path} is not valid UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"script_path is not readable: {script_path} ({exc})") from exc
    if not text.strip():
        raise ValueError(f"script file is empty: {script_path}")
    return script_path


def run_cpsat_python(
    source: str,
    *,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
) -> CpsatPythonResult:
    """Execute OR-Tools CP-SAT Python ``source`` in a child process.

    Writes ``source`` to a temporary file, runs it with ``sys.executable``
    (the server's own venv, which ships ``ortools``), and captures stdout/stderr
    to bounded temp files (max ``MAX_OUTPUT_BYTES`` each). Returns a
    ``CpsatPythonResult`` with the parsed solution and execution metadata.

    Raises ``ValueError`` on a non-positive ``timeout_ms`` — matching the
    MiniZinc path's ``_validate_model_and_timeout`` so a zero/negative cap is
    rejected up front rather than spawning a child only to kill it immediately.

    When a ``tracker`` is supplied (the server's per-run child tracker), the live
    child is registered for the duration of the run so an abrupt server teardown
    can terminate it instead of orphaning it; it is unregistered on every exit
    path (clean, timeout-kill, or output-cap kill).

    ``env`` is an INTERNAL environment overlay merged on top of the parent's
    environment for the child (callers are the experiment attempt runner and
    the seeded/configured save replay, which inject ``OPENCONSTRAINT_MCP_CPSAT_SEED``
    / ``_CONFIG``, or explicitly clear them via a ``None`` value — see
    ``seed_config_env``). It is NOT an MCP-facing parameter — the server never
    exposes arbitrary environment variables.

    For an existing local file, use ``run_cpsat_python_file`` instead — it runs
    the script in its own directory so relative file/import references resolve.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        script = tmp / "script.py"
        script.write_text(source, encoding="utf-8")
        # Run from the temp dir: an inline snippet has no sibling files to find.
        child = execute_child(
            _python_script_argv(script),
            cwd=tmp,
            timeout_ms=timeout_ms,
            tracker=tracker,
            on_start=on_start,
            env=env,
        )
        return _result_from_child(child)


def run_cpsat_python_file(
    script_path: Path,
    *,
    timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
) -> CpsatPythonResult:
    """Execute an existing OR-Tools CP-SAT Python file in its own directory.

    The path-based counterpart to ``run_cpsat_python``: instead of pasting the
    full source, the caller passes a local script path. The script runs with
    ``cwd`` set to its parent directory, so a relative ``open()`` of a sibling
    data file or ``import`` of a helper module resolves — the iteration win over
    copying the whole file inline. Mirrors the MiniZinc file tools
    (``solve_model_path``), which likewise run from the model's directory so a
    relative ``include`` resolves.

    Validates the path (exists / regular file / non-empty / UTF-8) with a clear
    ``ValueError`` before any child is spawned. Same execution contract, output
    cap, timeout, tree-kill, and INTERNAL ``env`` overlay (see ``run_cpsat_python``)
    as ``run_cpsat_python``.
    """
    resolved = _validate_script_path(script_path)
    child = execute_child(
        _python_script_argv(resolved),
        cwd=resolved.parent,
        timeout_ms=timeout_ms,
        tracker=tracker,
        on_start=on_start,
        env=env,
    )
    return _result_from_child(child)
