# openconstraint-mcp

A local-first [Model Context Protocol](https://modelcontextprotocol.io) server for
constraint programming and optimization. `openconstraint-mcp` wraps a **managed**
MiniZinc runtime (bundled and controlled by this project, not your system install)
and exposes open-source solvers — OR-Tools CP-SAT as the default, Chuffed as an
optional verifier — over MCP stdio. Privacy-first: no telemetry, no background
network calls, nothing leaves your machine unless you explicitly opt in.

## Installation

Requires Python 3.12+. This project is `uv`-managed end-to-end; install [`uv`](https://docs.astral.sh/uv/)
first if you don't already have it.

```bash
git clone https://github.com/KeLiu99/openconstraint-mcp.git
cd openconstraint-mcp
uv sync --all-groups
```

The `openconstraint-mcp` script will be available via `uv run openconstraint-mcp …`
(or `just cli …`, which wraps the same thing).

## CLI

The package exposes four commands:

- **`openconstraint-mcp stdio`** — run the MCP server over stdio. This is the entry
  point an MCP client (e.g. Claude Desktop, Claude Code) launches.
- **`openconstraint-mcp install-runtime`** — *placeholder in v0.* Will fetch and
  unpack a managed MiniZinc bundle in a later release. Currently prints a
  "not yet implemented" message and exits with code 1. See **v0 limitations**
  below for how to work around this in the meantime.
- **`openconstraint-mcp check-runtime`** — report whether the managed MiniZinc
  runtime is installed. Prints the expected runtime path and exits 0 when present,
  exits 1 otherwise.
- **`openconstraint-mcp list-solvers`** — list solvers exposed by the managed
  MiniZinc runtime. Requires the runtime to be installed; exits 1 with a clear
  error otherwise.

## MCP tools

The stdio server exposes two introspection tools:

- **`check_runtime`** — returns a `RuntimeStatus` with fields
  `installed: bool`, `runtime_dir: str`, and `minizinc_binary: str | None`.
- **`list_available_solvers`** — returns a `SolverList` of `SolverInfo` entries
  (`id`, `name`, `version`, `tags`). Raises a runtime-missing error if the
  managed MiniZinc binary is not present.

A `solve` / `optimize` tool is intentionally **not** part of v0 — see below.

## Managed runtime

The default managed runtime location is `<platformdirs user_data_dir>/minizinc`,
where `user_data_dir` comes from `PlatformDirs("openconstraint-mcp", "openconstraint-mcp")`.
Concretely:

| Platform | Default runtime root                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| Linux    | `~/.local/share/openconstraint-mcp/minizinc`                                           |
| macOS    | `~/Library/Application Support/openconstraint-mcp/minizinc`                            |
| Windows  | `%LOCALAPPDATA%\openconstraint-mcp\openconstraint-mcp\minizinc`                        |

> The doubled `openconstraint-mcp\openconstraint-mcp\…` segment on Windows is a
> `platformdirs` convention (appauthor *and* appname), not a path-computation bug.

The `minizinc` binary itself is expected at `<runtime>/bin/minizinc` (or
`<runtime>\bin\minizinc.exe` on Windows).

### Overriding the runtime path

Set the environment variable `OPENCONSTRAINT_MCP_RUNTIME_DIR` to override the
**runtime root directory** — *not* the path to the binary itself. The runtime
layer always appends `bin/minizinc` (or `bin\minizinc.exe`) underneath whatever
the env var points at.

For example, if your MiniZinc binary lives at `$HOME/minizinc-bundle/bin/minizinc`,
the correct override is:

```bash
export OPENCONSTRAINT_MCP_RUNTIME_DIR="$HOME/minizinc-bundle"
```

Setting `OPENCONSTRAINT_MCP_RUNTIME_DIR=/path/to/minizinc` directly (pointing at
the binary) will **not** work — the layer will look for `…/minizinc/bin/minizinc`
underneath it.

## v0 limitations

This is an early release; the focus is "easy install, reliable solving, clear
errors" rather than feature breadth. In particular:

- **The managed runtime is not yet auto-downloaded.** `install-runtime` is a
  placeholder. Until it lands, point `OPENCONSTRAINT_MCP_RUNTIME_DIR` at an
  existing MiniZinc install (a directory containing `bin/minizinc`) if you want
  `list-solvers` to work.
- **No `solve` / `optimize` MCP tool yet.** v0 only exposes introspection
  (`check_runtime`, `list_available_solvers`). The solve tool is the next
  iteration.
- **No telemetry, ever**, unless and until you explicitly opt in to a clearly
  labelled future feature.
- **No hidden network calls.** The package does not phone home; the only
  intentional network-using code path is the (not-yet-implemented)
  `install-runtime`. `httpx` is declared as a dependency in `pyproject.toml`
  but is reserved for that future work and is not imported anywhere in v0.
