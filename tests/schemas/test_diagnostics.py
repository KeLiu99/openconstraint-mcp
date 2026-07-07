from __future__ import annotations

import pytest

from openconstraint_mcp.schemas.diagnostics import (
    Diagnostic,
    checker_diagnostic,
    checker_status_is_failure,
    timeout_diagnostic,
    wrapper_job_diagnostic,
    wrapper_job_diagnostic_category,
)

# --- Diagnostic model -------------------------------------------------------


def test_diagnostic_details_defaults_to_none() -> None:
    diag = Diagnostic(category="infeasible", message="no solution exists")
    assert diag.details is None


def test_diagnostic_is_additive_json_safe() -> None:
    diag = Diagnostic(
        category="timeout_with_incumbent",
        message="hit the time limit",
        details={"timed_out": True, "return_code": 0, "solver": "cp-sat"},
    )
    # model_dump must round-trip through JSON scalars only.
    dumped = diag.model_dump()
    assert dumped == {
        "category": "timeout_with_incumbent",
        "message": "hit the time limit",
        "details": {"timed_out": True, "return_code": 0, "solver": "cp-sat"},
    }


# --- timeout_diagnostic -----------------------------------------------------


def test_timeout_diagnostic_with_incumbent() -> None:
    diag = timeout_diagnostic(has_incumbent=True)
    assert diag.category == "timeout_with_incumbent"


def test_timeout_diagnostic_without_incumbent() -> None:
    diag = timeout_diagnostic(has_incumbent=False)
    assert diag.category == "timeout_no_incumbent"


def test_timeout_diagnostic_carries_details() -> None:
    diag = timeout_diagnostic(has_incumbent=False, details={"timed_out": True})
    assert diag.details == {"timed_out": True}


# --- checker_status_is_failure / checker_diagnostic -------------------------


@pytest.mark.parametrize("clean_status", ["accepted", "completed"])
def test_checker_status_clean_is_not_failure(clean_status: str) -> None:
    assert checker_status_is_failure(clean_status) is False


@pytest.mark.parametrize("bad_status", ["violation", "no_solution", "error", "timeout", "rejected"])
def test_checker_status_bad_is_failure(bad_status: str) -> None:
    assert checker_status_is_failure(bad_status) is True


@pytest.mark.parametrize("clean_status", ["accepted", "completed"])
def test_checker_diagnostic_is_none_for_clean(clean_status: str) -> None:
    assert checker_diagnostic(clean_status) is None


def test_checker_diagnostic_maps_failure_to_checker_failed() -> None:
    diag = checker_diagnostic("rejected")
    assert diag is not None
    assert diag.category == "checker_failed"
    assert diag.details == {"checker_status": "rejected"}


def test_checker_diagnostic_merges_extra_details() -> None:
    diag = checker_diagnostic("violation", details={"attempt_index": 2})
    assert diag is not None
    assert diag.details == {"checker_status": "violation", "attempt_index": 2}


# --- wrapper_job_diagnostic_category / wrapper_job_diagnostic ----------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("cancelled", "cancelled"),
        ("failed", "job_failed"),
        # Result-bearing terminal states: the wrapper defers to the embedded
        # result's diagnostic, so the state-only mapping is None.
        ("succeeded", None),
        ("timeout", None),
        # Non-terminal states carry no diagnostic yet.
        ("queued", None),
        ("running", None),
    ],
)
def test_wrapper_job_diagnostic_category(state: str, expected: str | None) -> None:
    assert wrapper_job_diagnostic_category(state) == expected


def test_wrapper_job_diagnostic_builds_for_failed() -> None:
    diag = wrapper_job_diagnostic("failed", message="worker crashed", details={"job_id": "j1"})
    assert diag is not None
    assert diag.category == "job_failed"
    assert diag.message == "worker crashed"
    assert diag.details == {"job_id": "j1"}


def test_wrapper_job_diagnostic_is_none_for_result_bearing() -> None:
    assert wrapper_job_diagnostic("succeeded", message="ignored") is None
