from __future__ import annotations

import json
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.pyexec.jobs import CpsatJobRegistry
from openconstraint_mcp.server import (
    _homepage_url,
    _make_lifespan,
    _server_version,
    create_mcp_server,
    run_stdio,
)

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.shared.childproc import ChildProcessTracker
from openconstraint_mcp.shared.proc import popen_process_group


def _boot_lifespan() -> object:
    """A wired lifespan over fresh server-owned registries (boot tests)."""
    return _make_lifespan(JobRegistry(), CpsatJobRegistry(), ChildProcessTracker())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_teardown_terminates_in_flight_sync_child(
    fake_runtime_dir: Path,
) -> None:
    # The synchronous tools register their live child with the tracker; the
    # lifespan must terminate whatever is still in flight on teardown so it is
    # not orphaned, the same coverage background-job children already get.
    tracker = ChildProcessTracker()
    child = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker.register(child)
    lifespan = _make_lifespan(JobRegistry(), CpsatJobRegistry(), tracker)

    async with lifespan(create_mcp_server()):
        assert child.poll() is None  # still running within the server's lifetime

    assert child.wait(timeout=5) is not None  # terminated on teardown


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_teardown_reaps_sync_child_even_if_registry_shutdown_raises(
    fake_runtime_dir: Path,
) -> None:
    # The two teardown steps cover disjoint child sets and are independently
    # guarded: a failure tearing down the background-job registry must not skip
    # terminating the in-flight synchronous children, or they would be orphaned.
    class _BoomRegistry:
        def shutdown(self) -> None:
            raise RuntimeError("registry boom")

    tracker = ChildProcessTracker()
    child = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker.register(child)
    lifespan = _make_lifespan(_BoomRegistry(), CpsatJobRegistry(), tracker)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="registry boom"):
        async with lifespan(create_mcp_server()):
            assert child.poll() is None  # still running within the server's lifetime

    assert child.wait(timeout=5) is not None  # reaped despite the registry failure


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
    async with _boot_lifespan()(create_mcp_server()):
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
    async with _boot_lifespan()(create_mcp_server()):
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
    async with _boot_lifespan()(create_mcp_server()):
        pass

    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_lifespan_teardown_shuts_down_the_server_registry(
    fake_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server's own lifespan must terminate its job registry on exit (orphan
    # handling). Driving the wired lifespan and spying the class-level shutdown
    # proves create_mcp_server() bound the teardown to its registry.
    calls: list[bool] = []
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry.JobRegistry.shutdown",
        lambda self: calls.append(True),
    )
    server = create_mcp_server()
    lifespan = server.settings.lifespan
    assert lifespan is not None

    async with lifespan(server):
        assert calls == []
    assert calls == [True]


# --- toolset profiles ------------------------------------------------------

# The exact eight-tool core inventory. Pinned as a literal (not derived) so a
# schema reduction cannot hide accidental tool exposure and a new tool cannot
# silently join core.
CORE_TOOL_NAMES = {
    "check_runtime",
    "list_available_solvers",
    "check_minizinc_model",
    "solve_minizinc_model",
    "check_minizinc_files",
    "solve_minizinc_files",
    "run_cpsat_python",
    "run_cpsat_python_file",
}

# The complete current full-profile tool surface, pinned exactly.
FULL_TOOL_NAMES = CORE_TOOL_NAMES | {
    "inspect_minizinc_model",
    "find_unsat_core",
    "save_verified_minizinc_model",
    "inspect_minizinc_files",
    "find_unsat_core_files",
    "submit_solve_job",
    "get_solve_job",
    "cancel_solve_job",
    "list_solve_jobs",
    "submit_portfolio_job",
    "get_portfolio_job",
    "cancel_portfolio_job",
    "list_portfolio_jobs",
    "submit_cpsat_python_job",
    "submit_cpsat_python_file_job",
    "get_cpsat_python_job",
    "cancel_cpsat_python_job",
    "list_cpsat_python_jobs",
    "run_cpsat_python_experiment",
    "save_verified_cpsat_python",
    "load_tabular_data",
    "write_tabular_result",
}

FULL_PROMPT_NAMES = {
    "solve_constraint_problem",
    "solve_cpsat_python",
    "auto_tune_constraint_problem",
}

# A schema-change budget failure is a REVIEW trigger, not a cap to silently
# raise: reduce descriptions or reconsider the core inventory instead.
CORE_METADATA_BUDGET_BYTES = 40_000


def _serialize_tools(tools: list[Any]) -> str:
    """Deterministic compact serialization of a complete advertised tool list.

    One ``json.dumps`` over the whole list — ``model_dump(mode="json",
    exclude_none=True)`` per tool, sorted keys, compact separators — so the
    measured bytes include the list framing a client actually receives. Reused
    by the budget and reference-safety tests so they scan the same payload.
    """
    return json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        sort_keys=True,
        separators=(",", ":"),
    )


async def _tools_by_name(toolset: str) -> dict[str, Any]:
    tools = await create_mcp_server(toolset).list_tools()
    return {tool.name: tool for tool in tools}


@pytest.mark.asyncio
async def test_core_profile_exposes_exactly_the_eight_core_tools() -> None:
    tools = await _tools_by_name("core")
    assert set(tools) == CORE_TOOL_NAMES


@pytest.mark.asyncio
async def test_full_profile_retains_the_current_thirty_tool_set() -> None:
    tools = await _tools_by_name("full")
    assert set(tools) == FULL_TOOL_NAMES
    assert len(FULL_TOOL_NAMES) == 30


@pytest.mark.asyncio
async def test_core_profile_registers_no_prompts() -> None:
    prompts = await create_mcp_server("core").list_prompts()
    assert prompts == []


@pytest.mark.asyncio
async def test_full_profile_retains_the_three_prompts() -> None:
    prompts = await create_mcp_server("full").list_prompts()
    assert {prompt.name for prompt in prompts} == FULL_PROMPT_NAMES


def test_create_mcp_server_rejects_unknown_toolset() -> None:
    # The factory boundary rejects a bad value before any server is built,
    # independently of Typer's CLI-level validation, and names the accepted set.
    with pytest.raises(ValueError) as excinfo:
        create_mcp_server(toolset="typo")
    message = str(excinfo.value)
    assert "core" in message
    assert "full" in message


@pytest.mark.asyncio
async def test_full_profile_descriptions_advertise_full_only_cross_references() -> None:
    # Read from the actual full-profile registration (not the description
    # constants) so this verifies the wiring, not just the strings: the
    # conditional guidance the core variants drop must still be advertised here.
    tools = await _tools_by_name("full")
    assert "submit_portfolio_job" in tools["solve_minizinc_model"].description
    run_cpsat_python_desc = tools["run_cpsat_python"].description
    assert "solve_cpsat_python" in run_cpsat_python_desc
    assert "submit_portfolio_job" in run_cpsat_python_desc
    assert "save_verified_cpsat_python" in tools["run_cpsat_python_file"].description


@pytest.mark.asyncio
async def test_core_metadata_is_within_budget() -> None:
    tools = await create_mcp_server("core").list_tools()
    total = len(_serialize_tools(tools).encode("utf-8"))
    assert total <= CORE_METADATA_BUDGET_BYTES, (
        f"core metadata is {total} bytes, over the {CORE_METADATA_BUDGET_BYTES} budget"
    )


async def _forbidden_full_only_names() -> set[str]:
    """Every full-only tool name plus every full-profile prompt name.

    Derived from the live servers so it tracks reality: unavailable tools are
    ``full_names - core_names`` and prompts are all core-hidden.
    """
    full = create_mcp_server("full")
    full_names = {tool.name for tool in await full.list_tools()}
    prompt_names = {prompt.name for prompt in await full.list_prompts()}
    core_names = {tool.name for tool in await create_mcp_server("core").list_tools()}
    return (full_names - core_names) | prompt_names


@pytest.mark.asyncio
async def test_core_tool_payload_names_no_full_only_tool_or_prompt() -> None:
    # Scan the ENTIRE serialized core payload — description, inputSchema, and
    # outputSchema — so a full-only name reaching core through a Pydantic schema
    # docstring or field description is caught, not only a description mention.
    forbidden = await _forbidden_full_only_names()
    core_tools = await create_mcp_server("core").list_tools()
    payload = _serialize_tools(core_tools)
    leaked = sorted(name for name in forbidden if name in payload)
    assert leaked == []


@pytest.mark.asyncio
async def test_core_instructions_name_no_full_only_tool_or_prompt() -> None:
    forbidden = await _forbidden_full_only_names()
    instructions = create_mcp_server("core").instructions or ""
    leaked = sorted(name for name in forbidden if name in instructions)
    assert leaked == []


def test_run_stdio_defaults_to_core_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closes the gap between the mocked CLI tests and the direct factory
    # inventory tests: run_stdio() must request the core profile and run stdio.
    calls: list[tuple[str, str]] = []

    class _FakeServer:
        def run(self, *, transport: str) -> None:
            calls.append(("run", transport))

    def _fake_create(toolset: str = "full") -> _FakeServer:
        calls.append(("create", toolset))
        return _FakeServer()

    monkeypatch.setattr("openconstraint_mcp.server.create_mcp_server", _fake_create)
    run_stdio()
    assert calls == [("create", "core"), ("run", "stdio")]


def test_run_stdio_forwards_full_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class _FakeServer:
        def run(self, *, transport: str) -> None:
            calls.append(("run", transport))

    def _fake_create(toolset: str = "full") -> _FakeServer:
        calls.append(("create", toolset))
        return _FakeServer()

    monkeypatch.setattr("openconstraint_mcp.server.create_mcp_server", _fake_create)
    run_stdio(toolset="full")
    assert calls == [("create", "full"), ("run", "stdio")]
