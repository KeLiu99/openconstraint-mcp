from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .minizinc import list_solvers
from .runtime import RuntimeMissingError, get_runtime_status
from .server import run_stdio

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first MCP server for constraint programming, powered by MiniZinc.",
)
_console = Console()


@app.command()
def stdio() -> None:
    """Run the MCP server over stdio."""
    run_stdio()


@app.command("install-runtime")
def install_runtime() -> None:
    """Download and install the managed MiniZinc runtime (not yet implemented)."""
    _console.print(
        "[yellow]install-runtime is not yet implemented in v0.[/yellow] "
        "It will download and unpack a managed MiniZinc bundle in a future release."
    )
    raise typer.Exit(code=1)


@app.command("check-runtime")
def check_runtime() -> None:
    """Report whether the managed MiniZinc runtime is installed."""
    status = get_runtime_status()
    if status.installed:
        _console.print(
            f"[green]Runtime installed[/green] at {status.minizinc_binary}"
        )
        return
    _console.print(
        f"[red]Runtime not installed.[/red] Expected at {status.runtime_dir}"
    )
    raise typer.Exit(code=1)


@app.command("list-solvers")
def list_solvers_cmd() -> None:
    """List solvers available in the managed MiniZinc runtime."""
    try:
        result = list_solvers()
    except RuntimeMissingError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Available solvers")
    table.add_column("id")
    table.add_column("name")
    table.add_column("version")
    for solver in result.solvers:
        table.add_row(solver.id, solver.name, solver.version or "")
    _console.print(table)
