# AGENTS.md

Instructions for AI coding agents (Codex CLI, Claude Code, Cursor, etc.) working in this repository.

## Project

**openconstraint-mcp** is an open-source, local-first MCP server for constraint programming and optimization. It wraps a *managed* MiniZinc runtime (bundled and controlled by this project, not the user's system install) and exposes OSS solvers — OR-Tools CP-SAT as the default, Chuffed as an optional verifier — over the Model Context Protocol.

The repo is the open-source on-ramp for a commercial CP consulting/product business. The bar for v0 is "easy install, reliable solving, clear errors", not feature breadth.

## Architecture (v0)

```
cli  ─► server  ─► minizinc  ─► runtime  ─► schemas
                          ╲────────────────►  schemas
```

Modules (all under `src/openconstraint_mcp/`):

- `cli.py` — Typer entry point; user-facing commands (`stdio`, `install-runtime`, `check-runtime`, `list-solvers`).
- `server.py` — FastMCP app factory + stdio entry; the MCP tools live here.
- `minizinc.py` — subprocess wrapper around the managed `minizinc` binary.
- `runtime.py` — locates the managed runtime, reports status, owns `RuntimeMissingError`.
- `schemas.py` — Pydantic v2 result models.

## Before You Run Commands

**Always run `just --list` at the start of a session that will execute commands.** The `justfile` is the source of truth for project automation; prefer `just <recipe>` over raw `uv ...` invocations.

If `just` is unavailable in your environment, fall back to the underlying `uv run ...` commands the justfile uses. Do **not** invoke raw `python` or `pip` — this project is `uv`-managed end-to-end.

Common recipes:

| Recipe              | What it does                                            |
| ------------------- | ------------------------------------------------------- |
| `just` / `just list`| List all recipes.                                       |
| `just sync`         | Install/sync dependencies (incl. dev group).            |
| `just check`        | Lint + typecheck + tests. Run before declaring done.    |
| `just test`         | `pytest` only.                                          |
| `just lint`         | `ruff check .`                                          |
| `just format`       | `ruff format .` (writes changes).                       |
| `just typecheck`    | `mypy src`                                              |
| `just run`          | Start the MCP server on stdio.                          |
| `just cli <args>`   | Pass-through to the `openconstraint-mcp` CLI.           |
| `just clean`        | Remove caches and build artefacts.                      |

## Privacy & Network

- **Telemetry is disabled by default and is not implemented yet.** Do not add it. Any future telemetry must be opt-in, off by default, and clearly documented.
- **Nothing leaves the user's machine unless they explicitly opt in.** No background calls, no version checks, no analytics, no remote logging.
- **Runtime download is explicit and user-controlled.** `install-runtime` must only fetch when the user runs it; never on import, never on first `stdio` boot, never as a "convenience" auto-install.

## Code Style

- **Python 3.12+ compatible.** Development happens on Python 3.14, but the source must run on 3.12. Avoid 3.13+/3.14-only syntax and stdlib.
- **Type hints everywhere.** Public functions get full annotations. `mypy src` must pass.
- **Pydantic v2 models** for any structured input or output (MCP tool results, CLI structured output, config). Plain dicts are for ephemeral internal use only.
- **`pathlib.Path`** for filesystem work; do not pass raw strings around as paths.
- **Small, focused modules.** One responsibility per file; files that change together live together. Split by responsibility, not by technical layer.
- **No hidden network calls.** Network-using code is concentrated in `install-runtime` and any explicit user-invoked diagnostic upload.
- **Keep functions testable.** Inject dependencies (paths, subprocess runners, clocks) where doing so makes a function meaningfully easier to mock. Avoid global state.

## v0 Scope Guards

- **No Choco solver in v0.** Java/JAR friction; deferred (likely cloud-first).
- **No `solve` / `optimize` MCP tool in v0.** The skeleton ships `check_runtime` + `list_solvers` only.
- **No telemetry in v0.** See above.
- **No managed-runtime download in v0.** `install-runtime` is a placeholder that prints "not yet implemented".

## Testing

- **Framework: `pytest`** (with `pytest-asyncio` available but only used when needed).
- **Unit tests must not require a real MiniZinc runtime.** Use the `OPENCONSTRAINT_MCP_RUNTIME_DIR` env var + `tmp_path` pattern (see `tests/conftest.py`) to point the runtime layer at an empty directory.
- **Mock all network and subprocess in unit tests.** If a test genuinely needs to exercise a real binary, mark it `@pytest.mark.integration` and exclude it from the default `just check` until we wire an integration recipe.
- **One behaviour per test.** Long setup is fine; multi-assert telescopes are not.

## Documentation

- **Update `README.md`** when user-facing behaviour changes — new CLI command, new MCP tool, new flag, install steps.
- **Document managed-runtime behaviour:** where the MiniZinc bundle lives, how to override it (`OPENCONSTRAINT_MCP_RUNTIME_DIR`), and what version it pins.
- **Surface third-party licenses** for anything bundled (MiniZinc, OR-Tools, Chuffed) in a `LICENSES/` directory or an equivalent README section. Open-source compliance is non-negotiable.

## Definition of Done

A change is done when:

1. `just check` is green.
2. New behaviour has unit tests; non-trivial behaviour has at least one CLI- or MCP-level smoke test.
3. User-facing changes are reflected in `README.md`.
4. No new telemetry, no new hidden network calls, no new global mutable state.
