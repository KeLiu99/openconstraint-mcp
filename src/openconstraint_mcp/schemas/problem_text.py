from __future__ import annotations

import json
from typing import Annotated

from pydantic import BeforeValidator


def _coerce_problem_text(value: object) -> object:
    """Serialize a JSON object/array ``problem`` argument to its text form.

    ``problem`` is one text value: it is handed to the checker child as
    ``payload["problem"]`` and persisted verbatim as the saved ``problem.txt``.
    But a data-driven checker parses a machine-readable instance out of it, so
    callers routinely have a JSON object to send — and an MCP tool call is one
    JSON document, which means whether that value lands here as ``str`` or
    ``dict`` is decided by how the call was spelled, not by the declared
    annotation. Rejecting the object spelling fails a semantically correct call
    on a formatting detail the caller cannot reliably control, so accept it and
    serialize instead.

    Only ``dict``/``list`` are converted; a ``str`` passes through untouched. In
    particular a string that happens to contain quoted JSON is left alone —
    unwrapping it would corrupt an ordinary prose ``problem``.
    """
    if not isinstance(value, (dict, list)):
        return value
    try:
        # allow_nan=False: the checker payload is written as JSON, and Python
        # emits bare NaN/Infinity tokens that are not valid JSON. Reject here,
        # where the offending argument can still be named, rather than writing a
        # payload file only a lenient parser can read.
        # ensure_ascii=False: this text is persisted verbatim as problem.txt, and
        # a combined problem carries the user's original request alongside the
        # instance. Escaping it to \uXXXX round-trips through json.loads fine but
        # leaves the saved provenance unreadable for anyone whose request is not
        # pure ASCII, which is the artifact's whole purpose.
        return json.dumps(value, allow_nan=False, ensure_ascii=False)
    except ValueError as exc:
        raise ValueError(
            "problem must be JSON-serializable text; it contains a non-finite "
            f"number (NaN or ±inf): {exc}"
        ) from exc


ProblemText = Annotated[str | None, BeforeValidator(_coerce_problem_text)]
"""The ``problem`` tool parameter: text, or a JSON object/array serialized to text.

Includes ``None``. The published JSON schema stays ``string | null`` — the
canonical form callers should send — while the validator accepts the object
spelling as well.
"""
