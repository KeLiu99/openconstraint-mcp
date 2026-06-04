from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, NamedTuple

from ..schemas import SolveStatus

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


def _stringify_statistics(stats: dict[str, Any]) -> dict[str, str]:
    # Coerce a `statistics` object's typed JSON values (numbers, bools) to bare
    # strings. The caller `.update()`s the result, so duplicate keys across
    # successive statistics objects keep the last value (the old block-merge contract).
    return {
        str(key): value if isinstance(value, str) else str(value) for key, value in stats.items()
    }


def _extract_objective(variables: dict[str, Any]) -> int | float | None:
    """Pop ``_objective`` out of a solution's variable map and return it as a number.

    ``--output-objective`` injects ``_objective`` into every solution's json
    section, so it is removed from the public variable map here and surfaced
    separately as the objective. ``bool`` is rejected even though it subclasses
    ``int`` — a true/false is never a meaningful objective value. Returns None
    when ``_objective`` is absent or not a real number.
    """
    raw_objective = variables.pop("_objective", None)
    if isinstance(raw_objective, (int, float)) and not isinstance(raw_objective, bool):
        return raw_objective
    return None


def _human_block(output: dict[str, Any], variables: dict[str, Any] | None) -> str | None:
    """Pick the human-readable text for one solution.

    Prefers the model's own ``output.default`` rendering. When the model has no
    explicit ``output`` item the stream carries only the json section, so the
    block is synthesized from ``variables`` (already ``_objective``-free). Returns
    None when neither a ``default`` string nor any variables are present.
    """
    default_text = output.get("default")
    if isinstance(default_text, str):
        return default_text
    if variables:
        return _render_json_solution(variables)
    return None


class _SolutionParse(NamedTuple):
    # One `solution` stream object split into the three things the loop folds into
    # running state, so the parse and the accumulation stay separate concerns.
    # `variables`: `output.json` map with `_objective` removed; None if no json section.
    # `objective`: this solution's numeric `_objective`; None if absent/non-numeric.
    # `block`: human text for this solution; None when there is nothing to render.
    variables: dict[str, Any] | None
    objective: int | float | None
    block: str | None


def _parse_solution_object(obj: dict[str, Any]) -> _SolutionParse:
    """Split one ``solution`` stream object into its (variables, objective, block) parts.

    Returns all-None when the object carries no usable ``output`` map. ``block`` is
    ``output.default`` when present, else a synthesized rendering of ``variables`` —
    and None when neither exists (no ``default``, empty or absent json section).
    """
    output = obj.get("output")
    if not isinstance(output, dict):
        return _SolutionParse(None, None, None)
    json_section = output.get("json")
    variables = dict(json_section) if isinstance(json_section, dict) else None
    objective = _extract_objective(variables) if variables is not None else None
    return _SolutionParse(variables, objective, _human_block(output, variables))


class _StreamAccumulator:
    """Folds ``--json-stream`` objects into the fields of a `_StreamParse`.

    One ``_on_*`` handler per object ``type``; `consume` routes by type and ignores
    any unknown type (forward-compatible). The seven running fields live here rather
    than as locals in the parse loop, so each object type's effect is isolated and the
    loop itself stays a trivial fold.
    """

    def __init__(self) -> None:
        self._solutions: list[dict[str, Any]] = []
        self._blocks: list[str] = []
        self._statistics: dict[str, str] = {}
        self._messages: list[str] = []
        self._objective: int | float | None = None
        self._status_raw: str | None = None
        self._error_seen = False

    def consume(self, obj: dict[str, Any]) -> None:
        obj_type = obj.get("type")
        if obj_type == "solution":
            self._on_solution(obj)
        elif obj_type == "status":
            self._on_status(obj)
        elif obj_type == "statistics":
            self._on_statistics(obj)
        elif obj_type in ("error", "warning"):
            self._on_diagnostic(obj, is_error=obj_type == "error")
        # any other object type is ignored (forward-compatible)

    def _on_solution(self, obj: dict[str, Any]) -> None:
        sol = _parse_solution_object(obj)
        if sol.variables is not None:
            self._solutions.append(sol.variables)
        if sol.objective is not None:
            self._objective = sol.objective
        if sol.block is not None:
            self._blocks.append(sol.block)

    def _on_status(self, obj: dict[str, Any]) -> None:
        raw = obj.get("status")
        if isinstance(raw, str):
            self._status_raw = raw

    def _on_statistics(self, obj: dict[str, Any]) -> None:
        stats = obj.get("statistics")
        if isinstance(stats, dict):
            self._statistics.update(_stringify_statistics(stats))

    def _on_diagnostic(self, obj: dict[str, Any], *, is_error: bool) -> None:
        if is_error:
            self._error_seen = True
        line_msg = _diagnostic_line(obj)
        if line_msg is not None:
            self._messages.append(line_msg)

    def result(self) -> _StreamParse:
        # An `error` object (seen at any point) forces "error"; else the mapped
        # `{"type":"status"}` verdict; else None for the caller's return-code fallback.
        if self._error_seen:
            status: SolveStatus | None = "error"
        elif self._status_raw is not None:
            status = _map_status(self._status_raw, has_solution=bool(self._solutions))
        else:
            status = None
        return _StreamParse(
            status=status,
            solutions=self._solutions,
            objective=self._objective,
            statistics=self._statistics,
            stdout=_reconstruct_stdout(self._blocks),
            messages=self._messages,
        )


def _parse_solve_stream(stdout: str) -> _StreamParse:
    """Parse a ``--json-stream`` solve transcript into structured fields.

    Best-effort and never raises: a line that is not a JSON object (stray text, or a
    half-written final object truncated by a hard timeout) is skipped, and an unknown
    object ``type`` is ignored. ``_objective`` is removed from each solution's variable
    map, and the last solution's value becomes ``objective`` (None for satisfaction,
    where no solution carries one).
    """
    acc = _StreamAccumulator()
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # not a JSON object (truncated tail / stray text)
        if isinstance(obj, dict):
            acc.consume(obj)
    return acc.result()
