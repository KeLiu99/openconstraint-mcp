from __future__ import annotations

import json
from typing import Any

from openconstraint_mcp.minizinc.stream import _parse_solve_stream


def _stream(*objects: dict[str, Any]) -> str:
    return "".join(json.dumps(obj) + "\n" for obj in objects)


def _solution_obj(default: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "solution",
        "output": {"default": default, "raw": default, "json": values},
        "sections": ["default", "raw", "json"],
    }


def test_parse_solve_stream_maps_status_from_stream_object() -> None:
    # The verdict is read from the driver's own `{"type":"status"}` object, mapped
    # onto a SolveStatus literal — proving the parser lives in minizinc.stream.
    parsed = _parse_solve_stream(_stream({"type": "status", "status": "OPTIMAL_SOLUTION"}))
    assert parsed.status == "optimal"


def test_parse_solve_stream_strips_objective_from_solution_map() -> None:
    # `_objective` is removed from each solution's variable map and surfaced
    # separately as the numeric objective.
    parsed = _parse_solve_stream(
        _stream(
            _solution_obj("x=2 total=22\n", {"x": 2, "_objective": 22}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
    )
    assert parsed.solutions == [{"x": 2}]
    assert parsed.objective == 22
