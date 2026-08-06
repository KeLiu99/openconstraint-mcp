import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from examples.online_printing_shop.audit_instance import audit_instance
from examples.online_printing_shop.models import parse_input, read_input, solve
from openconstraint_mcp.server import create_mcp_server

ROOT = Path(__file__).parents[2]
EXAMPLE_DIR = ROOT / "examples" / "online_printing_shop"
INSTANCE_PATH = EXAMPLE_DIR / "data_sops1.json"


def load_instance() -> dict[str, Any]:
    return read_input(INSTANCE_PATH)


def test_sops1_instance_passes_semantic_validation_without_normalization() -> None:
    raw = load_instance()

    validated = parse_input(raw)

    assert validated.model_dump(mode="json", exclude_none=True) == raw


def test_sops1_model_proves_the_known_optimum() -> None:
    result = solve(parse_input(load_instance()))

    assert (result.status, result.objective) == ("optimal", 274)


def test_missing_successor_reference_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["1"]["successors"][0] = "missing-operation"

    with pytest.raises(ValidationError, match="unknown successor"):
        parse_input(raw)


def test_cyclic_precedence_graph_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["4"]["successors"] = ["1"]

    with pytest.raises(ValidationError, match="must be acyclic"):
        parse_input(raw)


def test_ineligible_fixed_machine_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["6"]["fixed"]["machine"] = "2"

    with pytest.raises(ValidationError, match="fixed machine is not eligible"):
        parse_input(raw)


def test_invalid_unavailability_interval_is_rejected() -> None:
    raw = load_instance()
    raw["machines"]["1"]["unavailability"][0] = {"start": 8, "end": 8}

    with pytest.raises(ValidationError, match="end must be greater than start"):
        parse_input(raw)


def test_unknown_field_is_rejected() -> None:
    raw = load_instance()
    raw["operations"]["4"]["objective"] = "makespan"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_input(raw)


def test_incomplete_setup_matrix_is_rejected() -> None:
    raw = load_instance()
    del raw["machines"]["1"]["setup_times"]["transitions"]["1"]["4"]

    with pytest.raises(ValidationError, match="must cover every other eligible operation"):
        parse_input(raw)


def test_legacy_instance_audit_accepts_complete_materialization() -> None:
    upstream = {
        "resources": [
            {
                "id": 1,
                "setup_size": [2, 3],
                "setup_color": 4,
                "setup_varnish": 5,
                "availability": [0, 10, 12, 20],
            }
        ],
        "jobs": [
            {
                "id": 1,
                "topology": [
                    {
                        "id": 1,
                        "starting": -1,
                        "release": 0,
                        "overlap": 1.0,
                        "size": 1,
                        "color": 1,
                        "varnish": 1,
                        "resources": [1],
                        "time": [7],
                        "sucessors": [],
                    }
                ],
            }
        ],
    }
    local = {
        "machines": {
            "1": {
                "unavailability": [{"start": 10, "end": 12}],
                "setup_times": {"first": {"1": 12}, "transitions": {}},
            }
        },
        "operations": {
            "1": {
                "job": "1",
                "successors": [],
                "machine_options": {"1": {"processing_time": 7}},
                "release_time": 0,
                "theta": 1.0,
            }
        },
    }

    assert audit_instance(upstream, local) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sops1_model_and_checker_reach_the_known_optimum_through_mcp() -> None:
    mcp = create_mcp_server("full")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(EXAMPLE_DIR / "models.py"),
            "checker_path": str(EXAMPLE_DIR / "checker.py"),
            "args": ["data_sops1.json"],
            "problem": "data_sops1.json",
            "timeout_ms": 30_000,
            "test_checker": True,
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content

    assert result["status"] == "optimal"
    assert result["objective"] == 274
    assert result["checker"]["status"] == "accepted", result["checker"]["errors"]
    assert result["checker_test"]["rejected_count"] == 4
    assert result["checker_test"]["accepted_count"] == 0

    payload = json.loads(result["stdout"].strip().splitlines()[-1])
    assert payload.keys() == {"status", "objective", "solution", "best_objective_bound"}
    schedule = payload["solution"]["schedule"]
    assert {entry["operation"] for entry in schedule} == set(load_instance()["operations"])
    fixed = next(entry for entry in schedule if entry["operation"] == "6")
    assert (fixed["machine"], fixed["start"]) == ("1", 79)
