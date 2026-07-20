from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_CHECKER_PATH = (
    Path(__file__).parent.parent / "examples" / "job_shop" / "checker.py"
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("job_shop_checker", _CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _valid_schedule() -> tuple[list[dict[str, int]], int]:
    """A trivially feasible schedule: every task laid end-to-end on one global
    clock, so no machine overlaps and every job's tasks stay in order. The
    makespan equals the sum of all durations."""
    schedule: list[dict[str, int]] = []
    clock = 0
    for job_id, job in enumerate(_checker.JOBS):
        for task_id, (machine, duration) in enumerate(job):
            schedule.append(
                {
                    "job": job_id,
                    "task": task_id,
                    "machine": machine,
                    "start": clock,
                    "duration": duration,
                    "end": clock + duration,
                }
            )
            clock += duration
    return schedule, clock


def _payload(objective: object) -> dict[str, Any]:
    schedule, _ = _valid_schedule()
    return {
        "problem": None,
        "solution": {"schedule": schedule},
        "objective": objective,
        "solver_status": "feasible",
    }


_MAKESPAN = _valid_schedule()[1]


@pytest.mark.parametrize("objective", [_MAKESPAN, float(_MAKESPAN)])
def test_accepts_objective_equal_to_makespan(objective: object) -> None:
    result = _checker.check_payload(_payload(objective))
    assert result["status"] == "accepted", result["errors"]


@pytest.mark.parametrize(
    "objective",
    [
        None,  # missing objective must not silently pass
        "55",  # numeric-looking string is still non-numeric
        "55.5",  # fractional string, likewise rejected
        _MAKESPAN + 0.9,  # fractional value that int() would truncate to a match
        True,  # bool is an int subclass but not a valid objective
        _MAKESPAN - 1,  # correct type, wrong value
    ],
)
def test_rejects_invalid_objective(objective: object) -> None:
    result = _checker.check_payload(_payload(objective))
    assert result["status"] == "rejected"
