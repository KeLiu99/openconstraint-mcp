from __future__ import annotations

import json
from typing import Any

from ..schemas.minizinc import InterfaceType, ModelInterface


def _build_interface_type(spec: dict[str, Any]) -> InterfaceType:
    """Map one MiniZinc interface entry onto the public ``InterfaceType`` fields.

    Explicit key mapping, not Pydantic aliases: MiniZinc's ``type`` -> ``base_type``,
    ``dim`` (absent for a scalar) -> ``dim``, ``set`` -> ``is_set``, ``optional``
    (from an ``opt`` type) -> ``is_optional``. The ``base_type`` Literal rejects a
    spelling outside the 2.9.7 vocab, so a renamed type fails loudly here rather
    than parsing into a silent default.
    """
    return InterfaceType(
        base_type=spec["type"],
        dim=int(spec.get("dim", 0)),
        is_set=bool(spec.get("set", False)),
        is_optional=bool(spec.get("optional", False)),
    )


def parse_model_interface(stdout: str) -> ModelInterface:
    """Parse a MiniZinc ``--model-interface-only`` stdout line into a ModelInterface.

    The runtime emits exactly one JSON object discriminated by ``"type":
    "interface"``. Unknown/extra top-level keys are ignored (forward-compatible);
    a missing required key (``input``/``output``/``method``/``has_output_item``),
    malformed JSON, a non-interface object, a malformed shape (a non-object
    ``input``/``output`` or entry value), or an unknown ``base_type``/``method``
    each raise a ``ValueError`` so the orchestrator can degrade to
    ``status="error"`` rather than mis-report a partial interface.
    """
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model-interface output is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("model-interface output is not a JSON object")
    if raw.get("type") != "interface":
        raise ValueError(f'expected a MiniZinc "interface" object, got type={raw.get("type")!r}')
    try:
        return ModelInterface(
            method=raw["method"],
            required_parameters={
                name: _build_interface_type(spec) for name, spec in raw["input"].items()
            },
            output_variables={
                name: _build_interface_type(spec) for name, spec in raw["output"].items()
            },
            has_output_item=raw["has_output_item"],
            globals=raw.get("globals", []),
            included_files=raw.get("included_files", []),
        )
    except KeyError as exc:
        # Only a missing key lands here. An unknown base_type/method raises pydantic
        # ValidationError (a ValueError subclass) from the constructors above, which
        # satisfies the same ValueError contract without an explicit catch.
        raise ValueError(f"model-interface output missing required key: {exc}") from exc
    except (AttributeError, TypeError) as exc:
        # `input`/`output` not a JSON object (no `.items()`) or an entry whose value
        # isn't a JSON object (`spec["type"]` on a non-dict). The real 2.9.7 binary
        # never emits these, but the degrade-to-error contract still must hold: fold
        # them into ValueError so the orchestrator reports status="error" instead of
        # leaking a raw AttributeError/TypeError past its `except ValueError`.
        raise ValueError(f"model-interface output has an unexpected shape: {exc}") from exc
