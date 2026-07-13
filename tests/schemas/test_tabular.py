from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.tabular import TabularData, TabularWriteResult


def _page(**overrides: Any) -> TabularData:
    """Build a valid EOF page, overriding only the fields under test."""
    fields: dict[str, Any] = {
        "headers": ["a"],
        "rows": [["x"]],
        "sheet_name": None,
        "available_sheets": [],
        "row_offset": 0,
        "next_row_offset": None,
        "total_rows": 1,
        "truncated": False,
        "truncation_reason": None,
    }
    fields.update(overrides)
    return TabularData(**fields)


# --- TabularCell: accepted scalars ---------------------------------------------


@pytest.mark.parametrize("cell", ["text", 42, 3.5, True, None])
def test_row_cell_accepts_every_json_scalar(cell: object) -> None:
    assert _page(rows=[[cell]]).rows == [[cell]]


def test_row_cell_preserves_bool_rather_than_coercing_it_to_int() -> None:
    # bool subclasses int, so a lax union would retype True as 1.
    assert _page(rows=[[True]]).rows[0][0] is True


def test_row_cell_does_not_coerce_a_numeric_string_to_a_number() -> None:
    assert _page(rows=[["5"]]).rows[0][0] == "5"


# --- TabularCell: rejected values ----------------------------------------------


@pytest.mark.parametrize("cell", [["nested"], {"key": "value"}])
def test_row_cell_rejects_a_nested_container(cell: object) -> None:
    with pytest.raises(ValidationError):
        _page(rows=[[cell]])


@pytest.mark.parametrize("cell", [float("inf"), float("-inf"), float("nan")])
def test_row_cell_rejects_a_non_finite_float(cell: float) -> None:
    with pytest.raises(ValidationError):
        _page(rows=[[cell]])


def test_headers_reject_a_non_string() -> None:
    with pytest.raises(ValidationError):
        _page(headers=[7])


# --- Pagination invariants ------------------------------------------------------


def test_truncated_page_carries_next_offset_and_reason() -> None:
    page = _page(next_row_offset=1, total_rows=2, truncated=True, truncation_reason="max_rows")
    assert (page.next_row_offset, page.truncation_reason) == (1, "max_rows")


def test_truncated_page_without_a_next_offset_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=None, truncated=True, truncation_reason="max_rows")


def test_truncated_page_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=True, truncation_reason=None)


def test_untruncated_page_with_a_next_offset_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=False, truncation_reason=None)


def test_untruncated_page_with_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=None, truncated=False, truncation_reason="max_bytes")


def test_unknown_truncation_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=True, truncation_reason="too_big")


# --- TabularWriteResult ---------------------------------------------------------


def test_write_result_status_defaults_to_written() -> None:
    result = TabularWriteResult(
        message="wrote 1 row",
        target_path="/tmp/out.csv",
        sha256="ab" * 32,
        format="csv",
        rows_written=1,
    )
    assert result.status == "written"


def test_write_result_rejects_an_unknown_format() -> None:
    with pytest.raises(ValidationError):
        TabularWriteResult(
            message="wrote 1 row",
            target_path="/tmp/out.ods",
            sha256="ab" * 32,
            format="ods",  # type: ignore[arg-type]
            rows_written=1,
        )
