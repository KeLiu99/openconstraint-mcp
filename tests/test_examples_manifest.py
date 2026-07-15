"""Manifest integrity for every saved example under ``tests/fixtures``.

Every ``.openconstraint-model.json`` in the repo is the output of
``save_verified_minizinc_model``/``save_verified_cpsat_python`` and lists its
sibling artifacts' sha256 hashes. This check needs no MiniZinc runtime or
``ortools`` solve: it only proves a shipped example was not hand-edited after
saving (which would silently invalidate the recorded verification), so it
runs in the default ``just check`` rather than requiring ``just integration``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.shared.hashing import path_sha256

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_MANIFESTS = sorted(_FIXTURES_DIR.rglob(".openconstraint-model.json"))


def test_at_least_one_manifest_is_tracked() -> None:
    assert _MANIFESTS, f"no .openconstraint-model.json found under {_FIXTURES_DIR}"


@pytest.mark.parametrize("manifest_path", _MANIFESTS, ids=[str(p) for p in _MANIFESTS])
def test_manifest_artifact_hashes_match_saved_files(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["artifacts"], f"{manifest_path} lists no artifacts"
    for artifact in manifest["artifacts"]:
        artifact_path = manifest_path.parent / artifact["path"]
        assert artifact_path.is_file(), f"{manifest_path} names missing file {artifact['path']}"
        assert path_sha256(artifact_path) == artifact["sha256"], (
            f"{artifact_path} no longer matches the sha256 recorded in {manifest_path} "
            "-- was it hand-edited after saving?"
        )
