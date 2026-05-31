from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .minizinc import MiniZincExecutionError, list_solvers
from .runtime import RuntimeMissingError, get_runtime_status, install_config_warning
from .server import run_stdio

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first MCP server for constraint programming, powered by MiniZinc.",
)
_console = Console()
_stderr_console = Console(stderr=True)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _warn_on_corrupt_install_config() -> None:
    """Surface a present-but-unparseable install config to stderr instead of
    silently falling back to the default runtime location."""
    warning = install_config_warning()
    if warning is not None:
        _stderr_console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command()
def stdio() -> None:
    """Run the MCP server over stdio."""
    run_stdio()


@app.command("install-runtime")
def install_runtime(
    runtime_dir: Path | None = typer.Option(  # noqa: B008  (typer-standard pattern)
        None,
        "--runtime-dir",
        help=(
            "Install location. Overrides $OPENCONSTRAINT_MCP_RUNTIME_DIR, the "
            "persisted install config, and the platformdirs default. Skips the "
            "interactive path prompt."
        ),
    ),
    yes: bool = typer.Option(  # noqa: B008  (typer-standard pattern)
        False,
        "--yes",
        "-y",
        help=(
            "Non-interactive: skip the path prompt and the overwrite-confirm "
            "prompt for a prior managed install. Required for non-TTY runs."
        ),
    ),
) -> None:
    """Download and install the managed MiniZinc runtime (Linux x86_64)."""
    # Lazy-imported so httpx/rich.progress stay out of stdio/check-runtime/list-solvers
    # cold paths. Enforced by test_cli_module_does_not_import_httpx_eagerly.
    from .runtime import get_runtime_dir, write_install_config
    from .runtime_install import (
        RuntimeInstallError,
        check_supported_platform,
        install_managed_runtime,
        is_managed_runtime_dir,
    )

    try:
        check_supported_platform()
    except RuntimeInstallError as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    _warn_on_corrupt_install_config()

    if runtime_dir is not None:
        target = runtime_dir
    else:
        default = get_runtime_dir()
        if _stdin_is_tty() and not yes:
            answer = typer.prompt(
                f"Install MiniZinc runtime at [{default}]",
                default=str(default),
                show_default=False,
            )
            target = Path(answer.strip() or str(default))
        else:
            target = default

    # Resolve once here so the CLI's pre-checks and error messages observe the
    # exact directory install_managed_runtime will operate on (it resolves too).
    target_resolved = target.expanduser().resolve()
    if target_resolved.exists() and not target_resolved.is_dir():
        _console.print(f"[red]Target exists but is not a directory: {target_resolved}[/red]")
        raise typer.Exit(code=1)

    effective_yes = yes
    if target_resolved.is_dir() and any(target_resolved.iterdir()):
        if not is_managed_runtime_dir(target_resolved):
            _console.print(
                f"[red]Refusing to install into {target_resolved}: this directory "
                "is not empty and does not look like a prior managed install. "
                "Pick an empty directory or remove the contents yourself.[/red]"
            )
            raise typer.Exit(code=1)
        if yes:
            effective_yes = True
        elif _stdin_is_tty():
            answer = typer.prompt(
                f"{target_resolved} is a prior managed runtime. Overwrite? [y/N]",
                default="n",
                show_default=False,
            )
            if answer.strip().lower() in {"y", "yes"}:
                effective_yes = True
            else:
                _console.print("aborted, nothing was changed")
                raise typer.Exit(code=0)
        else:
            _console.print(
                f"[red]{target_resolved} is a prior managed runtime; pass --yes "
                "to overwrite it non-interactively.[/red]"
            )
            raise typer.Exit(code=1)

    try:
        installed = install_managed_runtime(target_resolved, yes=effective_yes, console=_console)
    except (RuntimeInstallError, OSError) as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        write_install_config(installed)
    except OSError as exc:
        _console.print(
            f"[yellow]Runtime installed at {installed}, but could not save "
            f"the install config ({exc}). Set "
            f"OPENCONSTRAINT_MCP_RUNTIME_DIR={installed} or fix the config "
            f"directory and re-run install-runtime to persist the location."
            "[/yellow]"
        )


@app.command("configure-runtime")
def configure_runtime(
    runtime_dir: Path = typer.Option(  # noqa: B008  (typer-standard pattern)
        ...,
        "--runtime-dir",
        help=(
            "Path to an existing MiniZinc install (a directory containing "
            "bin/minizinc). The path is persisted so check-runtime and "
            "list-solvers find it without setting OPENCONSTRAINT_MCP_RUNTIME_DIR."
        ),
    ),
) -> None:
    """Point openconstraint-mcp at an existing MiniZinc install (no download)."""
    from .runtime import write_install_config

    target = runtime_dir.expanduser().resolve()
    if not target.is_dir():
        _console.print(f"[red]Not a directory: {target}[/red]")
        raise typer.Exit(code=1)

    binary_name = "minizinc.exe" if sys.platform == "win32" else "minizinc"
    binary = target / "bin" / binary_name
    if not binary.is_file():
        _console.print(
            f"[red]{target} does not look like a MiniZinc install: "
            f"expected {binary} to exist.[/red]"
        )
        raise typer.Exit(code=1)
    if sys.platform != "win32" and not os.access(binary, os.X_OK):
        _console.print(f"[red]{binary} is not executable.[/red]")
        raise typer.Exit(code=1)

    try:
        write_install_config(target)
    except OSError as exc:
        _console.print(
            f"[red]Could not save install config ({exc}). Set "
            f"OPENCONSTRAINT_MCP_RUNTIME_DIR={target} as a workaround.[/red]"
        )
        raise typer.Exit(code=1) from exc

    _console.print(f"[green]Configured runtime at {target}[/green]")


@app.command("check-runtime")
def check_runtime() -> None:
    """Report whether the managed MiniZinc runtime is installed."""
    _warn_on_corrupt_install_config()
    status = get_runtime_status()
    if status.installed:
        _console.print(f"[green]Runtime installed[/green] at {status.minizinc_binary}")
        return
    _console.print(f"[red]Runtime not installed.[/red] Expected at {status.runtime_dir}")
    raise typer.Exit(code=1)


@app.command("list-solvers")
def list_solvers_cmd() -> None:
    """List solvers available in the managed MiniZinc runtime."""
    _warn_on_corrupt_install_config()
    try:
        result = list_solvers()
    except (RuntimeMissingError, MiniZincExecutionError) as exc:
        _console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Available solvers")
    table.add_column("id")
    table.add_column("name")
    table.add_column("version")
    for solver in result.solvers:
        table.add_row(solver.id, solver.name, solver.version or "")
    _console.print(table)
