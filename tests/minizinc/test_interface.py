from __future__ import annotations

import pytest
from pydantic import ValidationError

from openconstraint_mcp.minizinc.interface import parse_model_interface
from openconstraint_mcp.schemas import (
    InterfaceType,
    ModelInspectionResult,
    ModelInterface,
)

# Captured verbatim from MiniZinc 2.9.7 `--model-interface-only` (see plan
# Investigation). stdout is exactly one line; the knapsack carries int/array
# params and a `maximize`; the globals object carries MiniZinc's cosmetic
# whitespace inside the `globals` array.
_KNAPSACK_INTERFACE = (
    '{"type": "interface", "input": {"n": {"type": "int"}, "capacity": '
    '{"type": "int"}, "weight": {"type": "int", "dim": 1}, "profit": '
    '{"type": "int", "dim": 1}}, "output": {"take": {"type": "int", "dim": 1}}, '
    '"method": "max", "has_output_item": true, "included_files": [], "globals": []}'
)

_GLOBALS_INTERFACE = (
    '{"type": "interface", "input": {"n": {"type": "int"}}, "output": '
    '{"q": {"type": "int", "dim": 1}}, "method": "sat", "has_output_item": '
    'false, "included_files": [], "globals": [    "alldifferent"]}'
)

# --- schema contract -------------------------------------------------------


def _ok_result() -> ModelInspectionResult:
    return ModelInspectionResult(
        status="ok",
        solver="cp-sat",
        interface=ModelInterface(
            method="max",
            required_parameters={"weight": InterfaceType(base_type="int", dim=1)},
            output_variables={"take": InterfaceType(base_type="int", dim=1)},
            has_output_item=True,
            globals=["alldifferent"],
            included_files=[],
        ),
        stdout="",
        stderr="",
        elapsed_ms=7,
    )


def test_inspection_result_dump_uses_field_names_not_minizinc_keys() -> None:
    # FastMCP serializes structured output via model_dump(mode="json", by_alias=True).
    # With no aliases that path must emit the public field names
    # base_type/is_set/is_optional — never MiniZinc's raw "type"/"set"/"optional" —
    # so outputSchema and structuredContent agree.
    dumped = _ok_result().model_dump(mode="json", by_alias=True)

    param = dumped["interface"]["required_parameters"]["weight"]
    assert set(param) == {"base_type", "dim", "is_set", "is_optional"}
    assert param["base_type"] == "int"
    assert "type" not in param
    assert "set" not in param


def test_interface_type_defaults_scalar_non_set_non_optional() -> None:
    scalar = InterfaceType(base_type="int")
    assert scalar.dim == 0
    assert scalar.is_set is False
    assert scalar.is_optional is False


def test_interface_type_rejects_unknown_base_type() -> None:
    # base_type is a Literal of MiniZinc 2.9.7's interface vocabulary, so a renamed
    # spelling fails loudly rather than silently mis-parsing.
    with pytest.raises(ValidationError):
        InterfaceType(base_type="enum")  # type: ignore[arg-type]


def test_model_interface_rejects_unknown_method() -> None:
    with pytest.raises(ValidationError):
        ModelInterface(
            method="bogus",  # type: ignore[arg-type]
            required_parameters={},
            output_variables={},
            has_output_item=False,
        )


def test_inspection_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        ModelInspectionResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_inspection_result_allows_none_interface_for_error() -> None:
    result = ModelInspectionResult(
        status="error",
        solver="cp-sat",
        stdout="",
        stderr="Error: type error\n",
        elapsed_ms=3,
    )
    assert result.interface is None


# --- parse_model_interface -------------------------------------------------


def test_parses_knapsack_interface() -> None:
    interface = parse_model_interface(_KNAPSACK_INTERFACE)

    assert interface.method == "max"
    assert interface.has_output_item is True
    assert set(interface.required_parameters) == {"n", "capacity", "weight", "profit"}
    # Scalars carry dim 0; the array params carry dim 1.
    assert interface.required_parameters["n"].dim == 0
    assert interface.required_parameters["weight"].dim == 1
    assert interface.required_parameters["profit"].dim == 1
    assert interface.output_variables["take"].dim == 1
    assert interface.output_variables["take"].base_type == "int"


def test_parses_globals_object_despite_cosmetic_whitespace() -> None:
    interface = parse_model_interface(_GLOBALS_INTERFACE)

    # MiniZinc pads the JSON array (`[    "alldifferent"]`); json.loads handles it,
    # so no hand-rolled string parsing is needed.
    assert interface.globals == ["alldifferent"]
    assert interface.method == "sat"
    assert interface.has_output_item is False


def test_parses_set_entry_as_is_set() -> None:
    stdout = (
        '{"type": "interface", "input": {"s": {"type": "int", "set": true}}, '
        '"output": {}, "method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters["s"].is_set is True
    assert interface.required_parameters["s"].dim == 0


def test_parses_opt_entry_as_is_optional() -> None:
    # MiniZinc reports an `opt` type as a base type plus an "optional": true flag,
    # the same modifier shape as "set" — so `opt int` maps to base_type int + is_optional.
    stdout = (
        '{"type": "interface", "input": {"g": {"type": "int", "optional": true}}, '
        '"output": {}, "method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters["g"].base_type == "int"
    assert interface.required_parameters["g"].is_optional is True
    assert interface.required_parameters["g"].is_set is False


def test_parses_tuple_and_record_base_types() -> None:
    # `tuple`/`record` are their own base-type tags in interface mode (no component
    # breakdown), so they parse rather than failing the whole inspection.
    stdout = (
        '{"type": "interface", "input": {"pt": {"type": "tuple"}}, '
        '"output": {"r": {"type": "record"}}, "method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters["pt"].base_type == "tuple"
    assert interface.output_variables["r"].base_type == "record"


def test_parses_ann_scalar_base_type() -> None:
    # `ann` (the annotation type) is its own base-type tag in interface mode. A
    # scalar ann entry carries no `dim`, so it parses as base_type "ann" at dim 0
    # rather than failing the whole inspection.
    stdout = (
        '{"type": "interface", "input": {"a": {"type": "ann"}}, '
        '"output": {}, "method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters["a"].base_type == "ann"
    assert interface.required_parameters["a"].dim == 0


def test_parses_ann_array_base_type() -> None:
    # An `array of ann` (e.g. a `seq_search` strategy list) reports base_type "ann"
    # with dim 1. Captured verbatim from MiniZinc 2.9.7, cosmetic `"type" : "ann"`
    # spaces and all — json.loads absorbs the padding, so no hand-parsing is needed.
    stdout = (
        '{"type": "interface", "input": {"strategies": {"type" : "ann", "dim" : 1}}, '
        '"output": {}, "method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters["strategies"].base_type == "ann"
    assert interface.required_parameters["strategies"].dim == 1


def test_parses_empty_input_as_complete() -> None:
    # An assigned par (`int: k = 3;`) and a model supplied with data both shrink
    # `input` to {}; at the parse boundary they are the same empty-input case, and
    # an empty required_parameters is the completeness signal.
    stdout = (
        '{"type": "interface", "input": {}, "output": {"x": {"type": "int"}}, '
        '"method": "sat", "has_output_item": false}'
    )
    interface = parse_model_interface(stdout)

    assert interface.required_parameters == {}


def test_rejects_non_interface_type() -> None:
    stdout = '{"type": "statistics", "statistics": {}}'
    with pytest.raises(ValueError, match="interface"):
        parse_model_interface(stdout)


def test_rejects_malformed_json() -> None:
    with pytest.raises(ValueError):
        parse_model_interface("not json at all")


def test_rejects_missing_required_key() -> None:
    # No "method" key — a missing required field is surfaced as a clear ValueError,
    # not a raw KeyError.
    stdout = '{"type": "interface", "input": {}, "output": {}, "has_output_item": false}'
    with pytest.raises(ValueError):
        parse_model_interface(stdout)


def test_rejects_non_dict_input_section() -> None:
    # `input`/`output` present but not a JSON object (here a list) has no `.items()`.
    # The degrade-to-error contract requires a ValueError, not a raw AttributeError
    # that escapes _build_inspection_result's `except ValueError` and crashes the tool.
    stdout = (
        '{"type": "interface", "input": [], "output": {}, "method": "sat", '
        '"has_output_item": false}'
    )
    with pytest.raises(ValueError):
        parse_model_interface(stdout)


def test_rejects_non_dict_entry_spec() -> None:
    # An entry whose value isn't a JSON object (here a bare string) makes
    # _build_interface_type evaluate `spec["type"]` on a non-dict -> TypeError. The
    # contract requires that surface as a ValueError so inspection degrades to error.
    stdout = (
        '{"type": "interface", "input": {"n": "int"}, "output": {}, '
        '"method": "sat", "has_output_item": false}'
    )
    with pytest.raises(ValueError):
        parse_model_interface(stdout)


def test_rejects_unknown_base_type() -> None:
    # A type spelling outside MiniZinc 2.9.7's interface vocab fails loudly via the
    # InterfaceBaseType Literal rather than silently mis-parsing. (enum collapses to
    # "int" in interface mode, so a literal "enum" tag is never emitted.)
    stdout = (
        '{"type": "interface", "input": {"x": {"type": "enum"}}, "output": {}, '
        '"method": "sat", "has_output_item": false}'
    )
    with pytest.raises(ValueError):
        parse_model_interface(stdout)


def test_rejects_unknown_method() -> None:
    stdout = (
        '{"type": "interface", "input": {}, "output": {}, "method": "bogus", '
        '"has_output_item": false}'
    )
    with pytest.raises(ValueError):
        parse_model_interface(stdout)
