from __future__ import annotations

from typing import Any

from openconstraint_mcp.minizinc.checker import _parse_checker_stream
from tests.minizinc.helpers import (
    VIOLATION_DIAGNOSTIC,
    checker_pass,
    checker_violation,
    solution_obj,
    stream,
)

# Captured `--json-stream --solution-checker` transcripts from the managed
# MiniZinc 2.9.7 runtime (org.gecode.gecode). One JSON object per line; the
# checker object is emitted immediately BEFORE the solution it validated, so the
# per-checker entries are positionally aligned with the solve parser's solutions.


def _pass_checker_nested_only(default_text: str) -> dict[str, Any]:
    # The documented checker shape (json-stream docs): the verdict lives ONLY in
    # the nested `messages[]` solution message, with NO top-level `output`. The
    # pinned 2.9.7 binary also hoists `output` to the top level, but the contract
    # does not guarantee it — so the verdict must still be recovered from here.
    return {
        "type": "checker",
        "messages": [
            {
                "type": "solution",
                "output": {"default": default_text, "raw": default_text},
                "sections": ["default", "raw"],
            }
        ],
    }


def test_parse_checker_stream_correct_verdict_has_no_violation() -> None:
    checks = _parse_checker_stream(
        stream(checker_pass("CORRECT\n"), solution_obj("x=1 y=2\n", {"x": 1, "y": 2}))
    )
    assert len(checks) == 1
    assert checks[0].violation is False
    assert checks[0].output == "CORRECT\n"


def test_parse_checker_stream_incorrect_text_is_not_adjudicated() -> None:
    # The author's "INCORRECT" text is a convention, not a runtime contract: the
    # server must surface it verbatim WITHOUT marking a violation. Only a nested
    # UNSATISFIABLE flips `violation`.
    checks = _parse_checker_stream(
        stream(checker_pass("INCORRECT\n"), solution_obj("x=3 y=1\n", {"x": 3, "y": 1}))
    )
    assert len(checks) == 1
    assert checks[0].violation is False
    assert checks[0].output == "INCORRECT\n"


def test_parse_checker_stream_recovers_verdict_from_nested_solution_only() -> None:
    # When the checker object omits the top-level `output` (the documented shape),
    # the verdict text must be recovered from the nested `type:"solution"` message's
    # `output.default` rather than falling through to absent diagnostics (which
    # would lose the verdict and leave `output` empty).
    checks = _parse_checker_stream(
        stream(
            _pass_checker_nested_only("CORRECT\n"),
            solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        )
    )
    assert len(checks) == 1
    assert checks[0].violation is False
    assert checks[0].output == "CORRECT\n"


def test_parse_checker_stream_constraint_violation_flags_true() -> None:
    checks = _parse_checker_stream(
        stream(checker_violation(), solution_obj("x=1 y=2\n", {"x": 1, "y": 2}))
    )
    assert len(checks) == 1
    assert checks[0].violation is True
    # The diagnostic text falls back from the nested warning message.
    assert checks[0].output == VIOLATION_DIAGNOSTIC


def test_parse_checker_stream_multi_solution_mix_preserves_order_and_flags() -> None:
    # The real `-a` transcript: two rejected solutions then one accepted, with a
    # checker object before each. Per-index violation flags must be [T, T, F] and
    # rejected solutions are still part of the stream (they remain in
    # solve.solutions — proved at the core/integration layer).
    checks = _parse_checker_stream(
        stream(
            {"type": "statistics", "statistics": {"method": "satisfy"}},
            checker_violation(),
            solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
            checker_violation(),
            solution_obj("x=1 y=3\n", {"x": 1, "y": 3}),
            checker_pass("checked x=2\n"),
            solution_obj("x=2 y=3\n", {"x": 2, "y": 3}),
            {"type": "status", "status": "ALL_SOLUTIONS"},
            {"type": "statistics", "statistics": {"nSolutions": 3}},
        )
    )
    assert [c.violation for c in checks] == [True, True, False]
    assert checks[0].output == VIOLATION_DIAGNOSTIC
    assert checks[2].output == "checked x=2\n"


def test_parse_checker_stream_no_checker_objects_returns_empty() -> None:
    # A solve transcript with solutions but no checker objects yields no checks;
    # the count mismatch (solutions present, checks empty) is the core's signal,
    # not the parser's — the leaf only reports what checker objects it saw.
    assert (
        _parse_checker_stream(
            stream(
                solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            )
        )
        == []
    )


def test_parse_checker_stream_skips_truncated_final_line() -> None:
    # A hard timeout can cut the final object mid-line; the unparseable tail is
    # skipped and the fully-received checker verdict is kept.
    truncated = stream(checker_pass("CORRECT\n")) + '{"type": "checker", "messa'
    checks = _parse_checker_stream(truncated)
    assert len(checks) == 1
    assert checks[0].output == "CORRECT\n"
