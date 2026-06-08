from __future__ import annotations

import pytest

from openconstraint_mcp.minizinc.unsat_core import _parse_unsat_core, _slice_source
from tests.minizinc.helpers import UNSAT_CORE_MODEL, UNSAT_CORE_STDOUT

# A clean findMUS transcript: a MUS line plus pipe-delimited trace spans, two from
# the entry model (model.mzn) and one from an included file (redefinitions.mzn).
_STDOUT = (
    "MUS: 1 2\nTraces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;redefinitions.mzn|10|1|10|5|\n"
)


def test_parse_unsat_core_extracts_only_entry_model_spans() -> None:
    # Proves the parser lives in unsat_core: spans are filtered to the entry
    # model's basename, so the included-file span is dropped from the structured core.
    mus_present, core = _parse_unsat_core(_STDOUT, UNSAT_CORE_MODEL)
    assert mus_present is True
    assert [(c.line, c.column, c.end_line, c.end_column) for c in core] == [
        (4, 12, 4, 20),
        (5, 12, 5, 20),
    ]


def test_parse_unsat_core_without_mus_line_returns_empty_core() -> None:
    # No `MUS:` line means no minimal unsatisfiable subset, even if trace spans exist.
    assert _parse_unsat_core("Traces: model.mzn|4|12|4|20|\n", UNSAT_CORE_MODEL) == (False, [])


def test_parse_unsat_core_extracts_model_spans() -> None:
    # The noisy real transcript (FznSubProblem/Brief preamble) is tolerated, the two
    # entry-model spans survive, the included-file span is dropped, and each span's
    # `source` carries the precise sliced constraint text.
    mus_present, core = _parse_unsat_core(UNSAT_CORE_STDOUT, UNSAT_CORE_MODEL)

    assert mus_present is True
    assert len(core) == 2
    assert core[0].line == 4
    assert core[0].column == 12
    assert core[0].end_line == 4
    assert core[0].end_column == 20
    assert "x + y > 5" in core[0].source
    assert "x + y < 3" in core[1].source
    assert all("x != y" not in item.source for item in core)


def test_parse_unsat_core_without_mus_returns_empty_core() -> None:
    assert _parse_unsat_core("=====UNKNOWN=====\n", UNSAT_CORE_MODEL) == (False, [])


# 1-indexed, end-inclusive spans over a 3-line model whose lines are each 5 chars.
_SLICE_MODEL = "abcde\nfghij\nklmno"


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec", "expected"),
    [
        pytest.param(1, 2, 1, 4, "bcd", id="single-line"),
        pytest.param(1, 3, 3, 2, "cde\nfghij\nkl", id="multi-line"),
    ],
)
def test_slice_source_returns_precise_span(
    sl: int, sc: int, el: int, ec: int, expected: str
) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == expected


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec"),
    [
        pytest.param(3, 1, 1, 1, id="start-after-end"),
        pytest.param(5, 1, 6, 2, id="start-past-eof"),
    ],
)
def test_slice_source_invalid_line_span_returns_empty(sl: int, sc: int, el: int, ec: int) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == ""


@pytest.mark.parametrize(
    ("sl", "sc", "el", "ec", "expected"),
    [
        pytest.param(2, 9, 2, 10, "fghij", id="column-past-line-end"),
        pytest.param(1, 4, 1, 2, "abcde", id="start-col-after-end-col"),
        pytest.param(2, 1, 5, 3, "fghij\nklmno", id="end-line-past-eof-clamped"),
    ],
)
def test_slice_source_falls_back_to_whole_lines(
    sl: int, sc: int, el: int, ec: int, expected: str
) -> None:
    assert _slice_source(_SLICE_MODEL, sl, sc, el, ec) == expected
