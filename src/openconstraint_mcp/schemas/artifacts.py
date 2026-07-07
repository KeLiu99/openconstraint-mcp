from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# The fixed artifact vocabulary of a saved verified-model directory. Filenames
# are fixed per role (model.mzn, data.dzn, checker.mzc.mzn, problem.md,
# solve-result.json, .openconstraint-model.json), so the role — not the
# filename — is the stable key clients branch on.
SavedArtifactRole = Literal[
    "model",
    "data",
    "checker",
    "problem",
    "solve_result",
    "solution",
    "manifest",
    "experiment_log",
    "replay_config",
]


class SavedModelArtifact(BaseModel):
    """One file written by a verified-model save.

    ``path`` is a bare filename relative to the saved directory — never an
    absolute path — matching the manifest's artifact convention. ``sha256`` is
    the hex digest of the file's bytes as written to disk.
    """

    role: SavedArtifactRole
    path: str
    sha256: str
