from __future__ import annotations

from openconstraint_mcp.protocol_text.status import SAVE_STAGES, cpsat_save_stages


def test_save_stages_unchanged_by_experiment_log_feature() -> None:
    # The experiment-log write (Tasks 3-4) rides inside the existing commit
    # stage; see the comment above SAVE_STAGES for why no new stage exists.
    assert SAVE_STAGES == (
        "Validating save request",
        "MiniZinc verification (check, then solve) and save are running",
        "MiniZinc finished; save decision made",
        "Save request complete",
    )


def test_cpsat_save_stages_unchanged_by_experiment_log_feature() -> None:
    assert cpsat_save_stages(with_checker=False) == (
        "Validating save request and CP-SAT Python source",
        "Re-running CP-SAT Python to evaluate the save gate",
        "Child finished; save decision made",
        "Save complete",
    )
    assert cpsat_save_stages(with_checker=True) == (
        "Validating save request and CP-SAT Python source",
        "Re-running CP-SAT Python, then the checker if earlier gates pass, "
        "to evaluate the save gate",
        "Child finished; save decision made",
        "Save complete",
    )
