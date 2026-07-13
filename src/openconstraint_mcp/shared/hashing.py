"""File-content hashing, as a dependency-light leaf.

Stdlib only. Split out of ``save_target`` so a module that needs to hash a
file it just wrote — the tabular writers, the artifact writers — does not have
to import the save-policy leaf and drag manifest/overwrite machinery along
with it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def path_sha256(path: Path) -> str:
    """Return the sha256 hex digest of the file at ``path``, streamed in chunks.

    ``hashlib.file_digest`` avoids holding the whole file in memory at once,
    unlike ``hashlib.sha256(path.read_bytes())``.
    """
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
