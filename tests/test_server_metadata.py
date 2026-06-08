from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _homepage_url,
    _lifespan,
    _server_version,
    create_mcp_server,
)

# --- website_url metadata --------------------------------------------------


def _expected_homepage_from_metadata() -> str | None:
    """Parse the ``Homepage`` Project-URL the same way the server should.

    Derived from live ``importlib.metadata`` so the test does not hardcode the
    URL literal: when the dedicated homepage launches, only ``pyproject.toml``
    changes and this expectation tracks it automatically.
    """
    for entry in metadata.metadata("openconstraint-mcp").get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage":
            return url.strip()
    return None


def test_homepage_url_returns_declared_homepage() -> None:
    url = _homepage_url()

    assert url is not None
    # Load-bearing: the comma-split leaves a leading space (' https://…'); this
    # assertion fails if the parse forgets to strip, catching a shared bug.
    assert url.startswith("https://")
    assert url == _expected_homepage_from_metadata()


def test_server_advertises_homepage_as_website_url() -> None:
    assert create_mcp_server().website_url == _homepage_url()


def test_homepage_url_none_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> object:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.metadata", _raise)

    assert _homepage_url() is None


def test_server_version_unknown_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> str:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.version", _raise)

    assert _server_version() == "unknown"


# --- lifespan boot diagnostic ----------------------------------------------


@pytest.mark.asyncio
async def test_boot_diagnostic_warns_when_runtime_missing(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _lifespan(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert _server_version() in err
    assert str(fake_runtime_dir) in err
    assert "NOT installed" in err
    assert "install-runtime" in err


@pytest.mark.asyncio
async def test_boot_diagnostic_reports_installed_runtime(
    fake_minizinc_binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _lifespan(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert "installed" in err
    assert str(fake_minizinc_binary) in err


@pytest.mark.asyncio
async def test_boot_diagnostic_writes_nothing_to_stdout(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Over stdio, stdout is the JSON-RPC channel; the banner must never land
    # there or it corrupts the protocol.
    async with _lifespan(create_mcp_server()):
        pass

    assert capsys.readouterr().out == ""


def test_lifespan_is_wired_into_server() -> None:
    assert create_mcp_server().settings.lifespan is _lifespan
