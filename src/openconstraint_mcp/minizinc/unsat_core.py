from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..schemas.minizinc import UnsatCoreConstraint


def _slice_source(model: str, sl: int, sc: int, el: int, ec: int) -> str:
    lines = model.splitlines()
    # Reject an unusable line span: before the file, inverted, or starting past EOF.
    if sl < 1 or el < 1 or sl > el or sl > len(lines):
        return ""

    # Clamp the end line into the file; the guard above keeps this non-empty.
    referenced = lines[sl - 1 : min(el, len(lines))]
    fallback = "\n".join(referenced)
    first, last = referenced[0], referenced[-1]

    # Slice precisely only when every column bound lands inside its line (and, on
    # a single line, start precedes end). An end line past EOF means the end
    # column can't be trusted, so fall back to the whole referenced line(s).
    cols_usable = (
        el <= len(lines)
        and 1 <= sc <= len(first)
        and 1 <= ec <= len(last)
        and (sl != el or sc <= ec)
    )
    if not cols_usable:
        return fallback

    if sl == el:
        return first[sc - 1 : ec]
    return "\n".join([first[sc - 1 :], *referenced[1:-1], last[:ec]])


# findMUS prints each MUS constraint as a pipe-delimited trace span:
# <file>|<start-line>|<start-col>|<end-line>|<end-col>|... — capture the file
# token (ending in .mzn) and the four 1-indexed coordinates.
_SPAN_PATTERN = re.compile(r"([^\s|;]+\.mzn)\|(\d+)\|(\d+)\|(\d+)\|(\d+)")


def _iter_model_spans(stdout: str, model_filename: str) -> Iterator[tuple[int, int, int, int]]:
    # Keep only spans whose file token matches the entry model's basename;
    # spans from included files stay out of the structured core. The match is
    # basename-only, so an included file sharing the entry model's basename in
    # a different directory would be mis-attributed here — a documented
    # best-effort limitation (raw stdout stays authoritative).
    for file_name, sl_raw, sc_raw, el_raw, ec_raw in _SPAN_PATTERN.findall(stdout):
        if Path(file_name).name == model_filename:
            yield int(sl_raw), int(sc_raw), int(el_raw), int(ec_raw)


def _constraint_from_span(model: str, span: tuple[int, int, int, int]) -> UnsatCoreConstraint:
    sl, sc, el, ec = span
    return UnsatCoreConstraint(
        line=sl,
        column=sc,
        end_line=el,
        end_column=ec,
        source=_slice_source(model, sl, sc, el, ec),
    )


def _parse_unsat_core(
    # Local default literal, decoupled from minizinc._MODEL_FILENAME to keep this
    # a leaf module: this is the parser's basename *filter*, while the temp-file
    # write target stays owned by minizinc (threaded in explicitly by
    # _build_unsat_core_result), so the two "model.mzn" literals play distinct roles.
    stdout: str,
    model: str,
    *,
    model_filename: str = "model.mzn",
) -> tuple[bool, list[UnsatCoreConstraint]]:
    mus_present = any(line.lstrip().startswith("MUS:") for line in stdout.splitlines())
    if not mus_present:
        return False, []

    # Repeated trace lines for the same constraint collapse to a single entry,
    # with first-seen order preserved (dict keeps insertion order).
    unique_spans = dict.fromkeys(_iter_model_spans(stdout, model_filename))
    return True, [_constraint_from_span(model, span) for span in unique_spans]
