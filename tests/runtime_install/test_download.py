from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from openconstraint_mcp.runtime_install.download import MINIZINC_VERSION, _download_archive
from openconstraint_mcp.runtime_install.errors import RuntimeInstallError


def test_module_exposes_version_constant() -> None:
    assert MINIZINC_VERSION == "2.9.7"


def test_runtime_install_error_is_runtime_error() -> None:
    assert issubclass(RuntimeInstallError, RuntimeError)


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> None:
    """Patch httpx.Client so it routes traffic through ``handler``.

    Capture the real ``httpx.Client`` *before* patching so the factory below can
    construct an actual client without recursing into itself.
    """
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]

    def _factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("openconstraint_mcp.runtime_install.download.httpx.Client", _factory)


def test_download_archive_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"fake bundle bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    _download_archive(
        "https://example.invalid/bundle.tgz",
        dest,
        expected_sha256=digest,
        console=Console(quiet=True),
    )
    assert dest.read_bytes() == payload


def test_download_archive_sha256_mismatch_deletes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"tampered bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    with pytest.raises(RuntimeInstallError) as exc_info:
        _download_archive(
            "https://example.invalid/bundle.tgz",
            dest,
            expected_sha256="0" * 64,
            console=Console(quiet=True),
        )
    assert "checksum" in str(exc_info.value).lower()
    assert not dest.exists()


def test_download_archive_http_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    _install_mock_transport(monkeypatch, handler)

    dest = tmp_path / "archive.tgz"
    with pytest.raises(RuntimeInstallError) as exc_info:
        _download_archive(
            "https://example.invalid/bundle.tgz",
            dest,
            expected_sha256="0" * 64,
            console=Console(quiet=True),
        )
    assert "download" in str(exc_info.value).lower()
    assert not dest.exists()
