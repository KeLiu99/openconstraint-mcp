"""Tests for the shared save-target validation and atomic commit primitives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from openconstraint_mcp.schemas import SavedModelArtifact
from openconstraint_mcp.shared.save_target import (
    MANIFEST_FILENAME,
    commit_staged_dir,
    path_sha256,
    text_sha256,
    validate_save_target,
)


def _minimal_writer(staging: Path) -> list[SavedModelArtifact]:
    """Write a minimal valid artifact set into staging; returns artifacts list."""
    content_path = staging / "result.txt"
    content_path.write_text("hello", encoding="utf-8")
    artifact = SavedModelArtifact(role="data", path="result.txt", sha256=path_sha256(content_path))
    manifest = {
        "managed_by": "openconstraint-mcp",
        "artifacts": [artifact.model_dump(mode="json")],
    }
    manifest_path = staging / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_artifact = SavedModelArtifact(
        role="manifest", path=MANIFEST_FILENAME, sha256=path_sha256(manifest_path)
    )
    return [artifact, manifest_artifact]


def _writer_with_content(content: str) -> Callable[[Path], list[SavedModelArtifact]]:
    """Return a writer that writes a result.txt with the given content."""

    def _writer(staging: Path) -> list[SavedModelArtifact]:
        content_path = staging / "result.txt"
        content_path.write_text(content, encoding="utf-8")
        artifact = SavedModelArtifact(
            role="data", path="result.txt", sha256=path_sha256(content_path)
        )
        manifest = {
            "managed_by": "openconstraint-mcp",
            "artifacts": [artifact.model_dump(mode="json")],
        }
        manifest_path = staging / MANIFEST_FILENAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_artifact = SavedModelArtifact(
            role="manifest", path=MANIFEST_FILENAME, sha256=path_sha256(manifest_path)
        )
        return [artifact, manifest_artifact]

    return _writer


# --- text_sha256 --------------------------------------------------------------


def test_text_sha256_matches_hashlib_over_utf8_bytes() -> None:
    text = "print('hello')\n"
    assert text_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_text_sha256_does_not_normalize_line_endings() -> None:
    # No newline normalization: "\n" and "\r\n" are distinct byte sequences and
    # must hash differently — this hashes the string exactly as given.
    assert text_sha256("a\nb") != text_sha256("a\r\nb")


# --- validate_save_target ---------------------------------------------------


def test_validate_save_target_rejects_relative_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_save_target(Path("relative/project"), overwrite=False)


def test_validate_save_target_rejects_existing_file_target(tmp_path: Path) -> None:
    file_target = tmp_path / "notadir"
    file_target.write_text("file")

    with pytest.raises(ValueError, match="not a directory"):
        validate_save_target(file_target, overwrite=False)


def test_validate_save_target_rejects_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parent directory"):
        validate_save_target(tmp_path / "missing" / "project", overwrite=False)


def test_validate_save_target_returns_resolved_path_for_new_dir(tmp_path: Path) -> None:
    target = validate_save_target(tmp_path / "project", overwrite=False)
    assert target == (tmp_path / "project").resolve()


def test_validate_save_target_accepts_empty_existing_dir(tmp_path: Path) -> None:
    # An existing empty directory is writable without overwrite — only
    # non-empty directories require the managed-save gate.
    target = tmp_path / "empty"
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
    commit_staged_dir(target, overwrite=False, write_files=_minimal_writer)

    with pytest.raises(ValueError, match="overwrite"):
        validate_save_target(target, overwrite=False)


def test_validate_save_target_allows_managed_dir_with_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "project"
    commit_staged_dir(target, overwrite=False, write_files=_minimal_writer)

    assert validate_save_target(target, overwrite=True) == target.resolve()


def test_validate_save_target_refuses_untracked_files_even_with_overwrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    commit_staged_dir(target, overwrite=False, write_files=_minimal_writer)
    (target / "notes.txt").write_text("user file the prior save did not write")

    with pytest.raises(ValueError, match="notes.txt"):
        validate_save_target(target, overwrite=True)


# --- commit_staged_dir -------------------------------------------------------


def test_commit_staged_dir_overwrite_replaces_wholesale(tmp_path: Path) -> None:
    # The replacement is whole-directory, not per-file: a file the prior
    # save wrote but the new save omits must be gone afterwards.
    target = tmp_path / "project"
    commit_staged_dir(target, overwrite=False, write_files=_writer_with_content("first"))

    def _writer_no_result(staging: Path) -> list[SavedModelArtifact]:
        # writes only the manifest (no result.txt)
        artifact: list[SavedModelArtifact] = []
        manifest = {"managed_by": "openconstraint-mcp", "artifacts": []}
        manifest_path = staging / MANIFEST_FILENAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        artifact.append(
            SavedModelArtifact(
                role="manifest", path=MANIFEST_FILENAME, sha256=path_sha256(manifest_path)
            )
        )
        return artifact

    commit_staged_dir(target, overwrite=True, write_files=_writer_no_result)

    assert not (target / "result.txt").exists()
    assert (target / MANIFEST_FILENAME).exists()


def test_commit_staged_dir_leaves_no_siblings_behind_on_success(tmp_path: Path) -> None:
    # Staging (and, for an overwrite, the backup) are transient: after a
    # successful commit the parent holds only the target.
    target = tmp_path / "project"
    commit_staged_dir(target, overwrite=False, write_files=_minimal_writer)
    commit_staged_dir(target, overwrite=True, write_files=_minimal_writer)

    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]


def test_commit_staged_dir_regate_refusal_cleans_staging(tmp_path: Path) -> None:
    # The overwrite gate re-runs immediately before commit; calling the writer
    # against a directory that turned non-empty-unmanaged must refuse, leave
    # the user files alone, and remove staging.
    target = tmp_path / "project"
    target.mkdir()
    (target / "thesis.tex").write_text("important")

    with pytest.raises(ValueError, match="not empty"):
        commit_staged_dir(target, overwrite=False, write_files=_minimal_writer)

    assert (target / "thesis.tex").read_text() == "important"
    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]


def test_commit_staged_dir_swap_failure_restores_prior_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    commit_staged_dir(target, overwrite=False, write_files=_writer_with_content("original"))
    prior_content = (target / "result.txt").read_text()

    real_rename = Path.rename

    def _flaky_rename(self: Path, dst: Path | str) -> Path:
        # Fail the staging→target swap itself; the move-aside and the restore
        # renames (plain target/backup names) pass through.
        if ".staging-" in self.name:
            raise OSError("simulated rename failure")
        return real_rename(self, dst)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    with pytest.raises(OSError, match="simulated"):
        commit_staged_dir(target, overwrite=True, write_files=_writer_with_content("replacement"))

    assert (target / "result.txt").read_text() == prior_content
    assert [entry.name for entry in tmp_path.iterdir()] == [target.name]
