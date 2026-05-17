from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .minizinc import list_solvers
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas import RuntimeStatus, SolverList


def create_server() -> FastMCP:
    mcp = FastMCP("openconstraint-mcp")

    @mcp.tool(description="Report whether the managed MiniZinc runtime is installed.")
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description="List solvers available in the managed MiniZinc runtime.")
    def list_available_solvers() -> SolverList:
        try:
            return list_solvers()
        except RuntimeMissingError as exc:
            # v0: surface the missing-runtime message as a plain MCP error so the
            # client sees something actionable. Future versions should return a
            # structured error envelope (e.g. {"code": "runtime_missing",
            # "hint": "run install-runtime"}) so MCP clients can branch on it
            # programmatically rather than parsing the message string.
            raise RuntimeError(str(exc)) from exc

    return mcp


def run_stdio() -> None:
    create_server().run(transport="stdio")
