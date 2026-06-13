from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from openconstraint_mcp.runtime_install.download import (
    MINIZINC_VERSION,
    _download_archive,
    select_bundle,
)
from openconstraint_mcp.runtime_install.errors import RuntimeInstallError


def _stub_platform(monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str) -> None:
    monkeypatch.setattr("openconstraint_mcp.runtime_install.download.sys.platform", platform_name)
    monkeypatch.setattr(
        "openconstraint_mcp.runtime_install.download.platform.machine", lambda: machine
    )


def test_module_exposes_version_constant() -> None:
    assert MINIZINC_VERSION == "2.9.7"


def test_runtime_install_error_is_runtime_error() -> None:
    assert issubclass(RuntimeInstallError, RuntimeError)


def test_select_bundle_linux_x86_64_resolves_tgz(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_platform(monkeypatch, "linux", "x86_64")
    bundle = select_bundle()
    assert bundle.kind == "tgz"
    assert bundle.filename == "MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz"
    assert bundle.url == (
        "https://github.com/MiniZinc/MiniZincIDE/releases/download/2.9.7/"
        "MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz"
    )
    assert bundle.sha256 == "7e78d3a1d6feec2f5b6a43628632decb6995755ade92ff4e51a2188c54ca6399"


def test_select_bundle_macos_arm64_resolves_dmg(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_platform(monkeypatch, "darwin", "arm64")
    bundle = select_bundle()
    assert bundle.kind == "dmg"
    assert bundle.filename == "MiniZincIDE-2.9.7-bundled.dmg"
    assert bundle.url == (
        "https://github.com/MiniZinc/MiniZincIDE/releases/download/2.9.7/"
        "MiniZincIDE-2.9.7-bundled.dmg"
    )
    assert bundle.sha256 == "504d04d3315f2a76455b71feff2cc2b3105ecd5533e8194fa2365bc41289d9d9"


@pytest.mark.parametrize(
    ("platform_name", "machine"),
    [
        ("darwin", "x86_64"),
        ("linux", "aarch64"),
        ("win32", "AMD64"),
    ],
)
def test_select_bundle_rejects_unsupported_platforms(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    machine: str,
) -> None:
    _stub_platform(monkeypatch, platform_name, machine)
    with pytest.raises(RuntimeInstallError) as exc_info:
        select_bundle()
    message = str(exc_info.value)
    assert "Linux x86_64" in message
    assert "macOS arm64" in message
    assert "configure-runtime" in message


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
