from __future__ import annotations

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.artifacts import SavedModelArtifact


def test_saved_model_artifact_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        SavedModelArtifact(
            role="readme",  # type: ignore[arg-type]
            path="README.md",
            sha256="ef" * 32,
        )
