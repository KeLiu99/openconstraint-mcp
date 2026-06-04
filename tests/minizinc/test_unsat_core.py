from __future__ import annotations

from openconstraint_mcp.minizinc.unsat_core import _parse_unsat_core

_MODEL = (
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "\n"
    "constraint x + y > 5;\n"
    "constraint x + y < 3;\n"
    "constraint x != y;\n"
    "\n"
    "solve satisfy;\n"
)

# A findMUS transcript: a MUS line plus pipe-delimited trace spans, two from the
# entry model (model.mzn) and one from an included file (redefinitions.mzn).
_STDOUT = (
    "MUS: 1 2\nTraces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;redefinitions.mzn|10|1|10|5|\n"
)


def test_parse_unsat_core_extracts_only_entry_model_spans() -> None:
    # Proves the parser lives in unsat_core: spans are filtered to the entry
    # model's basename, so the included-file span is dropped from the structured core.
    mus_present, core = _parse_unsat_core(_STDOUT, _MODEL)
    assert mus_present is True
    assert [(c.line, c.column, c.end_line, c.end_column) for c in core] == [
        (4, 12, 4, 20),
        (5, 12, 5, 20),
    ]


def test_parse_unsat_core_without_mus_line_returns_empty_core() -> None:
    # No `MUS:` line means no minimal unsatisfiable subset, even if trace spans exist.
    assert _parse_unsat_core("Traces: model.mzn|4|12|4|20|\n", _MODEL) == (False, [])
