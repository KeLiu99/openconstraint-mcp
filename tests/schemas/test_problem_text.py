from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from openconstraint_mcp.schemas.problem_text import ProblemText

_adapter: TypeAdapter[str | None] = TypeAdapter(ProblemText)


def test_json_object_is_serialized_to_text() -> None:
    instance = {"request": "schedule ft06", "num_machines": 6, "jobs": [[[2, 1], [0, 3]]]}
    result = _adapter.validate_python(instance)
    assert isinstance(result, str)
    assert json.loads(result) == instance


def test_json_array_is_serialized_to_text() -> None:
    assert _adapter.validate_python([[2, 1], [0, 3]]) == "[[2, 1], [0, 3]]"


def test_string_passes_through_unchanged() -> None:
    assert _adapter.validate_python("Schedule six jobs on six machines.") == (
        "Schedule six jobs on six machines."
    )


def test_string_containing_json_is_not_unwrapped() -> None:
    # Unwrapping would corrupt a prose problem that happens to be quoted; the
    # already-serialized form is what the checker expects to json.loads once.
    text = '{"num_machines": 6}'
    assert _adapter.validate_python(text) == text


def test_non_ascii_request_survives_unescaped() -> None:
    # The serialized text is persisted verbatim as problem.txt. \uXXXX escapes
    # would round-trip through json.loads but leave that provenance unreadable.
    request = "Schedule — café naïve 六机"
    result = _adapter.validate_python({"request": request, "num_machines": 6})
    assert isinstance(result, str)
    assert request in result
    assert "\\u" not in result
    assert json.loads(result)["request"] == request


def test_none_passes_through() -> None:
    assert _adapter.validate_python(None) is None


def test_non_finite_number_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-finite number"):
        _adapter.validate_python({"num_machines": float("nan")})


def test_published_schema_stays_string_or_null() -> None:
    # The canonical form callers should send is text; accepting the object
    # spelling is runtime leniency, not an advertised second shape.
    assert _adapter.json_schema() == {"anyOf": [{"type": "string"}, {"type": "null"}]}
