"""Public models for the tabular (``.xlsx``/``.csv``) I/O tools.

The server does mechanical I/O only: it never infers what a column *means*.
A cell is therefore a JSON scalar and nothing more — see ``TabularCell``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

# One spreadsheet cell. Strict members on purpose:
#
# * ``bool`` subclasses ``int``, so a lax union would coerce ``True`` to ``1``
#   and silently retype the cell. Strict members match only their exact type.
# * ``allow_inf_nan=False`` rejects ``inf``/``nan``, which have no JSON form —
#   without it they would fail at *serialization*, i.e. after a write already
#   touched the disk. Rejecting them here keeps the failure pre-I/O.
#
# Lists and objects match no member and are rejected the same way.
type TabularCell = (
    StrictStr | StrictBool | StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)] | None
)

TabularFormat = Literal["xlsx", "csv"]
TabularTruncationReason = Literal["max_rows", "max_bytes"]


class TabularData(BaseModel):
    """One bounded page of rows read from a spreadsheet or CSV file.

    ``headers`` are always strings (see ``shared.tabular_io`` for the
    normalization rules) and are repeated on every page, so a page is
    self-describing. ``row_offset`` is a zero-based offset among *data* rows —
    the header row, when present, is not a data row.

    Pagination invariant: ``truncated`` is true exactly when rows remain, and
    in that case both ``next_row_offset`` (the offset to request next) and
    ``truncation_reason`` (which bound stopped this page) are set. At EOF all
    three are ``None``/``False``.
    """

    headers: list[str]
    rows: list[list[TabularCell]]
    sheet_name: str | None
    available_sheets: list[str]
    row_offset: int
    next_row_offset: int | None
    total_rows: int
    truncated: bool
    truncation_reason: TabularTruncationReason | None

    @model_validator(mode="after")
    def _check_pagination(self) -> TabularData:
        if self.truncated and (self.next_row_offset is None or self.truncation_reason is None):
            raise ValueError(
                "a truncated page must carry both next_row_offset and truncation_reason"
            )
        if not self.truncated and (
            self.next_row_offset is not None or self.truncation_reason is not None
        ):
            raise ValueError(
                "an untruncated page must carry neither next_row_offset nor truncation_reason"
            )
        return self


class TabularWriteResult(BaseModel):
    """The outcome of a successful tabular write.

    Only produced on success — every refusal (bad path, non-scalar cell,
    ragged row, refused to overwrite) raises instead. ``sha256`` is the digest
    of the staged file's bytes, computed before the commit publishes them —
    identical to the committed file's bytes, since the commit is a rename/link
    of that same staged file.
    """

    status: Literal["written"] = "written"
    message: str
    target_path: str
    sha256: str
    format: TabularFormat
    rows_written: int
