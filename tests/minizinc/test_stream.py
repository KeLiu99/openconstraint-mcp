from __future__ import annotations

import pytest

from openconstraint_mcp.minizinc.stream import _parse_solve_stream
from openconstraint_mcp.schemas.minizinc import SolveStatus
from tests.minizinc.helpers import (
    STREAM_ERROR,
    STREAM_OPTIMAL_MULTI,
    STREAM_SATISFY_ALL,
    STREAM_UNSAT,
    solution_obj,
    solution_obj_json_only,
    stream,
)


def test_parse_solve_stream_maps_status_from_stream_object() -> None:
    # The verdict is read from the driver's own `{"type":"status"}` object, mapped
    # onto a SolveStatus literal — proving the parser lives in minizinc.stream.
    parsed = _parse_solve_stream(stream({"type": "status", "status": "OPTIMAL_SOLUTION"}))
    assert parsed.status == "optimal"


def test_parse_solve_stream_strips_objective_from_solution_map() -> None:
    # `_objective` is removed from each solution's variable map and surfaced
    # separately as the numeric objective.
    parsed = _parse_solve_stream(
        stream(
            solution_obj("x=2 total=22\n", {"x": 2, "_objective": 22}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
    )
    assert parsed.solutions == [{"x": 2}]
    assert parsed.objective == 22


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("OPTIMAL_SOLUTION", "optimal"),
        ("ALL_SOLUTIONS", "satisfied"),
        ("SATISFIED", "satisfied"),
        ("UNSATISFIABLE", "unsatisfiable"),
        ("UNKNOWN", "unknown"),
        ("UNBOUNDED", "unbounded"),
        ("UNSAT_OR_UNBOUNDED", "unsat_or_unbounded"),
        # A driver/solver runtime-failure verdict (e.g. cp-sat rejecting an
        # out-of-range `random_seed`) must surface as "error", not fall through
        # to "unknown" and hide the failure.
        ("ERROR", "error"),
    ],
)
def test_parse_solve_stream_maps_known_status(verdict: str, expected: SolveStatus) -> None:
    # Status is read from the driver's own `{"type":"status"}` object (never from
    # text); each verified/enum spelling maps onto a SolveStatus literal.
    assert _parse_solve_stream(stream({"type": "status", "status": verdict})).status == expected


def test_parse_solve_stream_unknown_status_falls_back_safely() -> None:
    # A renamed or newly added MiniZinc verdict never crashes a solve: with a
    # solution in hand it reads as satisfied, otherwise unknown.
    with_solution = stream(
        solution_obj("x=1\n", {"x": 1}),
        {"type": "status", "status": "FUTURE_VERDICT"},
    )
    without_solution = stream({"type": "status", "status": "FUTURE_VERDICT"})
    assert _parse_solve_stream(with_solution).status == "satisfied"
    assert _parse_solve_stream(without_solution).status == "unknown"


def test_parse_solve_stream_strips_objective_and_orders_solutions() -> None:
    # `solutions` preserves emission order with `_objective` removed from each;
    # `objective` is the last (best) solution's `_objective`.
    parsed = _parse_solve_stream(STREAM_OPTIMAL_MULTI)
    assert parsed.solutions == [{"x": 0, "y": 0}, {"x": 0, "y": 2}, {"x": 2, "y": 10}]
    assert all("_objective" not in solution for solution in parsed.solutions)
    assert parsed.objective == 22
    assert parsed.status == "optimal"


def test_parse_solve_stream_rejects_bool_objective() -> None:
    # `_objective` is stripped from the public solution map whatever its type, but a
    # bool is never accepted as the numeric objective even though bool subclasses
    # int — so objective stays None while the variable map is still cleaned.
    parsed = _parse_solve_stream(
        stream(
            solution_obj("x=1\n", {"x": 1, "_objective": True}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
    )
    assert parsed.objective is None
    assert parsed.solutions == [{"x": 1}]


def test_parse_solve_stream_satisfaction_has_no_objective() -> None:
    # A satisfy model's json section carries no `_objective`, so objective is None
    # even though solutions are present.
    parsed = _parse_solve_stream(STREAM_SATISFY_ALL)
    assert parsed.objective is None
    assert parsed.solutions[-1] == {"x": 2, "y": 3}
    assert parsed.status == "satisfied"


def test_parse_solve_stream_merges_statistics_last_wins() -> None:
    # Typed JSON stat values become bare strings; duplicate keys across objects
    # keep the last value (mirroring the old block-merge contract).
    stdout = stream(
        {"type": "statistics", "statistics": {"method": "maximize", "objective": 0}},
        {"type": "statistics", "statistics": {"objective": 22, "flatTime": 0.04, "failures": 0}},
    )
    assert _parse_solve_stream(stdout).statistics == {
        "method": "maximize",
        "objective": "22",
        "flatTime": "0.04",
        "failures": "0",
    }


def test_parse_solve_stream_reconstructs_stdout_from_default_sections() -> None:
    # The human stdout is rebuilt from each solution's `output.default`, one
    # newline-terminated block per solution — not the raw json-stream bytes.
    assert _parse_solve_stream(STREAM_SATISFY_ALL).stdout == "x=1 y=2\nx=1 y=3\nx=2 y=3\n"


def test_parse_solve_stream_synthesizes_stdout_when_only_json_section() -> None:
    # A model with no explicit `output` item emits a solution object carrying only
    # the `json` section. The human stdout is synthesized from the variable map
    # (with `_objective` stripped) so a real no-output solve still shows a solution
    # instead of an empty stdout.
    parsed = _parse_solve_stream(
        stream(
            solution_obj_json_only({"x": 5, "_objective": 5}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
    )
    assert parsed.solutions == [{"x": 5}]
    assert parsed.objective == 5
    assert parsed.stdout == "x = 5;\n"
    # The internal objective artifact never leaks into the human text.
    assert "_objective" not in parsed.stdout


def test_parse_solve_stream_skips_truncated_final_line() -> None:
    # A hard timeout can cut the final object mid-line; the unparseable tail is
    # skipped and the fully-received solution/objective are kept.
    truncated = (
        stream(
            solution_obj("x=2 y=10 total=22\n", {"x": 2, "y": 10, "_objective": 22}),
            {"type": "status", "status": "OPTIMAL_SOLUTION"},
        )
        + '{"type": "statistics", "statistics": {"objec'
    )
    parsed = _parse_solve_stream(truncated)
    assert parsed.status == "optimal"
    assert parsed.solutions == [{"x": 2, "y": 10}]
    assert parsed.objective == 22


def test_parse_solve_stream_surfaces_error_and_warning_messages() -> None:
    # An error object forces status "error" and its message is collected; a
    # warning contributes its message without changing the verdict.
    assert _parse_solve_stream(STREAM_ERROR).status == "error"
    assert _parse_solve_stream(STREAM_ERROR).messages == [
        "syntax error: unexpected item, expecting ';' or end of file"
    ]
    assert _parse_solve_stream(STREAM_UNSAT).messages == ["model inconsistency detected"]
