from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openconstraint_mcp.server import create_server


@pytest.mark.asyncio
async def test_list_available_solvers_surfaces_actionable_error_on_binary_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[str(fake_minizinc_binary), "--solvers-json"],
            stderr="bad config\n",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.subprocess.run", _fake_run)

    mcp = create_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("list_available_solvers", {})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "bad config" in message
