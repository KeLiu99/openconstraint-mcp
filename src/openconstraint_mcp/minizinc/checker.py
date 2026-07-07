from __future__ import annotations

import json
from typing import Any

from ..schemas.minizinc import SolutionCheck

# MiniZinc's `--solution-checker` emits one `{"type":"checker"}` object per
# produced solution, immediately BEFORE that solution, on the same `--json-stream`
# transcript the solve parser reads. This leaf walks that transcript independently
# of `stream.py` (sibling leaves do not import each other — AGENTS.md), emitting
# one `SolutionCheck` per checker object. Because each checker precedes its
# solution and both parsers preserve emission order, the resulting list is
# positionally aligned with `_parse_solve_stream`'s `solutions`; the core compares
# the two counts to detect drift. The checker object is self-contained: a clean
# run carries the author verdict text on its nested `type:"solution"` message's
# `output.default` (the documented json-stream shape; the pinned runtime also
# hoists it to the checker's top-level `output.default`), while a constraint-style
# rejection carries a nested `{"type":"status","status":"UNSATISFIABLE"}` plus a
# warning diagnostic and no rendered output.


def _checker_violation(messages: list[Any]) -> bool:
    # The one machine-readable "this solution is invalid" signal: a nested status
    # object reporting UNSATISFIABLE (a constraint-style checker's hard rejection).
    return any(
        isinstance(msg, dict)
        and msg.get("type") == "status"
        and msg.get("status") == "UNSATISFIABLE"
        for msg in messages
    )


def _section_default(obj: Any) -> str | None:
    # A rendered `output.default` section, when present as a string.
    output = obj.get("output") if isinstance(obj, dict) else None
    if isinstance(output, dict) and isinstance(output.get("default"), str):
        return output["default"]
    return None


def _checker_output(obj: dict[str, Any], messages: list[Any]) -> str:
    # Prefer the checker's own rendered verdict text (author CORRECT/INCORRECT…),
    # surfaced verbatim and NOT interpreted. The pinned runtime hoists it to the
    # checker's top-level `output.default`, but the documented json-stream contract
    # places it on the nested `type:"solution"` message instead — so check both
    # before falling back to the diagnostic message(s) a rejection carries.
    top_level = _section_default(obj)
    if top_level is not None:
        return top_level
    # Documented checker shape:
    # {"messages": [{"type": "solution", "output": {"default": "CORRECT\n"}}]}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") != "solution":
            continue
        nested = _section_default(msg)
        if nested is not None:
            return nested
    diagnostics = [
        msg["message"]
        for msg in messages
        if isinstance(msg, dict) and isinstance(msg.get("message"), str)
    ]
    return "\n".join(diagnostics)


def _parse_checker_object(obj: dict[str, Any]) -> SolutionCheck:
    raw_messages = obj.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    return SolutionCheck(
        violation=_checker_violation(messages),
        output=_checker_output(obj, messages),
    )


def _parse_checker_stream(stdout: str) -> list[SolutionCheck]:
    """Parse a `--solution-checker` transcript into one ``SolutionCheck`` per checker.

    Best-effort and never raises: a line that is not a JSON object (stray text, or a
    half-written final object truncated by a hard timeout) is skipped, and any object
    type other than ``checker`` is ignored. Returns ``[]`` for a transcript with no
    checker objects — the count-vs-solutions mismatch that implies is the core's
    derivation, not this leaf's concern.
    """
    checks: list[SolutionCheck] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # not a JSON object (truncated tail / stray text)
        if isinstance(obj, dict) and obj.get("type") == "checker":
            checks.append(_parse_checker_object(obj))
    return checks
