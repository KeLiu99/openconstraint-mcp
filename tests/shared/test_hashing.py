"""Tests for the shared file-hashing leaf."""

from __future__ import annotations

import hashlib
from pathlib import Path

from openconstraint_mcp.shared.hashing import path_sha256


def test_path_sha256_matches_hashlib_over_the_files_bytes(tmp_path: Path) -> None:
    target = tmp_path / "model.mzn"
    payload = b"var 1..3: x;\nsolve satisfy;\n"
    target.write_bytes(payload)
    assert path_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_path_sha256_hashes_bytes_not_decoded_text(tmp_path: Path) -> None:
    # Line endings are a byte-level difference: a file hashed from disk must
    # reflect exactly what is on disk, with no newline normalization.
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(b"a\nb")
    crlf.write_bytes(b"a\r\nb")
    assert path_sha256(lf) != path_sha256(crlf)


def test_path_sha256_of_an_empty_file_is_the_empty_digest(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")
    assert path_sha256(target) == hashlib.sha256(b"").hexdigest()
