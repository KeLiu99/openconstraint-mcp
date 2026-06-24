from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.artifacts import (
    CHECKER_FILENAME,
    DATA_FILENAME,
    MANIFEST_FILENAME,
    MODEL_FILENAME,
    PROBLEM_FILENAME,
    SOLVE_RESULT_FILENAME,
    write_verified_model_dir,
)
from openconstraint_mcp.save_target import validate_save_target
from openconstraint_mcp.schemas import CheckResult, SolveResult

_MODEL = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"

_DEFAULT_CONTROLS: dict[str, Any] = {
    "timeout_ms": 30_000,
    "free_search": False,
    "parallel": None,
    "random_seed": None,
    "all_solutions": False,
    "num_solutions": None,
}


def _check_ok() -> CheckResult:
    return CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=4)


def _solve_satisfied() -> SolveResult:
    return SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=3\n",
        stderr="",
        elapsed_ms=11,
        solution={"x": 3},
        solutions=[{"x": 3}],
        objective=None,
    )


def _write_dir(target: Path, **overrides: Any) -> Any:
    """Call write_verified_model_dir with happy-path defaults."""
    kwargs: dict[str, Any] = {
        "model": _MODEL,
        "data": None,
        "checker": None,
        "problem": None,
        "check": _check_ok(),
        "solve": _solve_satisfied(),
        "solve_controls": dict(_DEFAULT_CONTROLS),
        "overwrite": False,
    }
    kwargs.update(overrides)
    return write_verified_model_dir(target, **kwargs)


# --- validate_save_target ----------------------------------------------------


def test_validate_save_target_rejects_relative_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_save_target(Path("relative/project"), overwrite=False)


def test_validate_save_target_rejects_existing_file_target(tmp_path: Path) -> None:
    file_target = tmp_path / "occupied"
    file_target.write_text("not a directory")

    with pytest.raises(ValueError, match="not a directory"):
        validate_save_target(file_target, overwrite=False)


def test_validate_save_target_rejects_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parent"):
        validate_save_target(tmp_path / "missing" / "project", overwrite=False)


def test_validate_save_target_returns_resolved_path_for_new_dir(tmp_path: Path) -> None:
    target = validate_save_target(tmp_path / "project", overwrite=False)
    assert target == (tmp_path / "project").resolve()


def test_validate_save_target_accepts_empty_existing_dir(tmp_path: Path) -> None:
    # An existing empty directory is writable without overwrite — only
    # *contents* are protected, not the directory entry itself.
    target = tmp_path / "project"
    target.mkdir()

    assert validate_save_target(target, overwrite=False) == target.resolve()


def test_validate_save_target_refuses_nonempty_unmanaged_dir(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / "thesis.tex").write_text("important")

    with pytest.raises(ValueError, match="not empty"):
        validate_save_target(target, overwrite=True)


def test_validate_save_target_refuses_dir_with_corrupt_manifest(tmp_path: Path) -> None:
    # Fail-closed: a manifest that does not parse as ours makes the directory
    # unmanaged, so even overwrite=True refuses rather than guessing which
    # files are safe to replace.
    target = tmp_path / "project"
    target.mkdir()
    (target / MANIFEST_FILENAME).write_text("{not json")

    with pytest.raises(ValueError, match="not empty"):
        validate_save_target(target, overwrite=True)


def test_validate_save_target_refuses_managed_dir_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "project"
    _write_dir(target)

    with pytest.raises(ValueError, match="overwrite"):
        validate_save_target(target, overwrite=False)


def test_validate_save_target_allows_managed_dir_with_overwrite(tmp_path: Path) -> None:
    # A prior save lists its artifacts in the manifest; the marker itself does
    # not count as untracked, so the gate passes with overwrite=True.
    target = tmp_path / "project"
    _write_dir(target)

    assert validate_save_target(target, overwrite=True) == target.resolve()


def test_validate_save_target_refuses_untracked_files_even_with_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    _write_dir(target)
    (target / "notes.txt").write_text("user file the prior save did not write")

    with pytest.raises(ValueError, match="notes.txt"):
        validate_save_target(target, overwrite=True)


# --- write_verified_model_dir -------------------------------------------------


def test_write_verified_model_dir_writes_minimal_layout(tmp_path: Path) -> None:
    target = tmp_path / "project"

    files, warning = _write_dir(target)

    assert warning is None
    assert (target / MODEL_FILENAME).read_text() == _MODEL
    assert sorted(entry.name for entry in target.iterdir()) == sorted(
        [MODEL_FILENAME, SOLVE_RESULT_FILENAME, MANIFEST_FILENAME]
    )
    assert [(artifact.role, artifact.path) for artifact in files] == [
        ("model", MODEL_FILENAME),
        ("solve_result", SOLVE_RESULT_FILENAME),
        ("manifest", MANIFEST_FILENAME),
    ]


def test_write_verified_model_dir_writes_optional_artifacts_when_supplied(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"

    files, _ = _write_dir(
        target,
        data="n = 3;\n",
        checker='output ["CORRECT"];\n',
        problem="Pick the best x.\n",
    )

    assert (target / DATA_FILENAME).read_text() == "n = 3;\n"
    assert (target / CHECKER_FILENAME).read_text() == 'output ["CORRECT"];\n'
    assert (target / PROBLEM_FILENAME).read_text() == "Pick the best x.\n"
    assert [artifact.role for artifact in files] == [
        "model",
        "data",
        "checker",
        "problem",
        "solve_result",
        "manifest",
    ]


def test_write_verified_model_dir_writes_empty_data_file(tmp_path: Path) -> None:
    # An empty string is a valid "no parameters" data input (matching the
    # inline solve contract) and still produces its file.
    target = tmp_path / "project"

    _write_dir(target, data="")

    assert (target / DATA_FILENAME).read_text() == ""


def test_write_verified_model_dir_hashes_match_disk_contents(tmp_path: Path) -> None:
    target = tmp_path / "project"

    files, _ = _write_dir(target, data="n = 3;\n")

    for artifact in files:
        on_disk = hashlib.sha256((target / artifact.path).read_bytes()).hexdigest()
        assert artifact.sha256 == on_disk, f"hash mismatch for {artifact.path}"


def test_write_verified_model_dir_manifest_records_provenance(tmp_path: Path) -> None:
    target = tmp_path / "project"
    controls = dict(_DEFAULT_CONTROLS, timeout_ms=5_000, free_search=True)

    files, _ = _write_dir(target, solve_controls=controls)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["managed_by"] == "openconstraint-mcp"
    assert "tool_version" in manifest
    datetime.fromisoformat(manifest["created_at"])  # absolute ISO timestamp
    assert manifest["solver"] == "cp-sat"
    assert manifest["solve_controls"] == controls
    assert manifest["verification"] == {
        "check_status": "ok",
        "solve_status": "satisfied",
        "checker_status": None,
    }
    # The manifest lists every file except itself; the result list appends the
    # manifest as its final entry (hashed after write — it cannot self-hash).
    assert manifest["artifacts"] == [artifact.model_dump(mode="json") for artifact in files[:-1]]
    assert files[-1].role == "manifest"


def test_write_verified_model_dir_solve_result_json_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "project"
    solve = _solve_satisfied()

    _write_dir(target, solve=solve)

    payload = json.loads((target / SOLVE_RESULT_FILENAME).read_text())
    assert payload == solve.model_dump(mode="json")


def test_write_verified_model_dir_overwrite_replaces_wholesale(tmp_path: Path) -> None:
    # The replacement is whole-directory, not per-file: a data.dzn the prior
    # save wrote but the new save omits must be gone afterwards.
    target = tmp_path / "project"
    _write_dir(target, data="n = 3;\n")

    new_model = "var 1..9: y;\nsolve satisfy;\n"
    _write_dir(target, model=new_model, overwrite=True)

    assert (target / MODEL_FILENAME).read_text() == new_model
    assert not (target / DATA_FILENAME).exists()


def test_write_verified_model_dir_leaves_no_siblings_behind_on_success(
    tmp_path: Path,
) -> None:
    # Staging (and, for an overwrite, the backup) are transient: after a
    # successful commit the parent holds only the target.
    target = tmp_path / "project"
    _write_dir(target)
    _write_dir(target, overwrite=True)

    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]


def test_write_verified_model_dir_regate_refusal_cleans_staging(tmp_path: Path) -> None:
    # The overwrite gate re-runs immediately before commit; calling the writer
    # against a directory that turned non-empty-unmanaged (as it could during a
    # long solve) must refuse, leave the user files alone, and remove staging.
    target = tmp_path / "project"
    target.mkdir()
    (target / "thesis.tex").write_text("important")

    with pytest.raises(ValueError, match="not empty"):
        _write_dir(target)

    assert (target / "thesis.tex").read_text() == "important"
    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]


def test_write_verified_model_dir_swap_failure_restores_prior_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    _write_dir(target)
    prior_model = (target / MODEL_FILENAME).read_text()

    real_rename = Path.rename

    def _flaky_rename(self: Path, dst: Path | str) -> Path:
        # Fail the staging→target swap itself; the move-aside and the restore
        # renames (plain target/backup names) pass through.
        if ".staging-" in self.name:
            raise OSError("simulated rename failure")
        return real_rename(self, dst)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(OSError, match="simulated"):
        _write_dir(target, model="var 1..2: z;\nsolve satisfy;\n", overwrite=True)

    assert (target / MODEL_FILENAME).read_text() == prior_model
    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]
