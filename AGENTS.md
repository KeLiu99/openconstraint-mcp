# AGENTS.md

Instructions for AI coding agents (Codex CLI, Claude Code, Cursor, etc.) working in this repository.

## Project

**openconstraint-mcp** is an open-source, local-first MCP server for constraint programming and optimization. It wraps a *managed* MiniZinc runtime (bundled and controlled by this project, not the user's system install) and exposes OSS solvers — OR-Tools CP-SAT as the default, Chuffed as an optional verifier — over the Model Context Protocol.

The repo is the open-source on-ramp for a commercial CP consulting/product business. The bar for v0 is "easy install, reliable solving, clear errors", not feature breadth.

## Working Principles

### 1. Think Before Coding

State assumptions explicitly. Present multiple interpretations when the request is ambiguous instead of silently picking one. Push back when a simpler approach exists. Stop and name what's unclear rather than guessing.

### 2. Simplicity First

Write the minimum code that solves the stated problem. No speculative features, no abstractions for single-use code, no configurability that wasn't requested, no error handling for impossible scenarios. If 200 lines could be 50, rewrite it. Test: would a senior engineer call this overcomplicated?

### 3. Surgical Changes

Touch only what the task requires. Don't "improve" adjacent code, comments, or formatting. Match existing style even if you'd do it differently. Flag unrelated dead code — don't delete it. Remove imports/variables/functions that *your* changes orphaned; leave pre-existing dead code alone. Every changed line should trace to the user's request.

### 4. Goal-Driven Execution

Define success criteria before starting; loop until verified.

| Instead of...    | Transform to...                                       |
| ---------------- | ----------------------------------------------------- |
| "Add validation" | "Write tests for invalid inputs, then make them pass" |
| "Fix the bug"    | "Write a test that reproduces it, then make it pass"  |
| "Refactor X"     | "Ensure tests pass before and after"                  |

For multistep tasks, state a brief plan with a verification check per step.

## Architecture (v0)

```
cli  ──►  server  ──►  minizinc  ──►  runtime  ──►  schemas
```

A module may import any module to its right. Imports never flow leftward or between same-layer modules.

## Before You Run Commands

**Always run `just --list` at the start of a session that will execute commands.** The `justfile` is the source of truth for project automation; prefer `just <recipe>` over raw `uv ...` invocations.

If `just` is unavailable in your environment, fall back to the underlying `uv run ...` commands the justfile uses. Do **not** invoke raw `python` or `pip` — this project is `uv`-managed end-to-end.

## Privacy & Network

- **Telemetry is not implemented.** Do not add it. Any future telemetry must be opt-in and documented.
- **Nothing leaves the user's machine without explicit opt-in** — no background calls, version checks, analytics, or remote logging.
- **Runtime download is user-invoked only.** `install-runtime` fetches when the user runs it — never on import, on first `stdio` boot, or as a "convenience" auto-install.

## Code Style

- **Target Python 3.12** (development happens on 3.14). Avoid 3.13+ syntax and stdlib.
- **Type hints everywhere.** Public functions get full annotations. `mypy src` must pass.
- **Pydantic v2 models** for any structured input or output (MCP tool results, CLI structured output, config). Plain dicts are for ephemeral internal use only.
- **`pathlib.Path`** for filesystem work; do not pass raw strings around as paths.
- **One responsibility per file.** Files that change together live; split by responsibility, not by technical layer.
- **Keep functions testable.** Inject dependencies (paths, subprocess runners, clocks) where it makes a function meaningfully easier to mock. Avoid global state.

## v0 Scope Guards

- **No Choco solver in v0.** Java/JAR friction; deferred (likely cloud-first).
- **No `solve` / `optimize` MCP tool in v0.** The skeleton ships `check_runtime` + `list_solvers` only.
- **No managed-runtime download in v0.** `install-runtime` is a placeholder that prints "not yet implemented".

## Testing

- **Framework: `pytest`** (with `pytest-asyncio` available but only used when needed).
- **Unit tests must not require a real MiniZinc runtime.** Use the `OPENCONSTRAINT_MCP_RUNTIME_DIR` env var + `tmp_path` pattern (see `tests/conftest.py`) to point the runtime layer at an empty directory.
- **Mock all network and subprocess in unit tests.** Real-binary tests get `@pytest.mark.integration` and stay out of the default `just check`.
- **One behavior per test.** Long setup is fine; multi-assert telescopes are not.

## Documentation

- **Update `README.md`** when user-facing behaviour changes — new CLI command, new MCP tool, new flag, install steps.
- **Document managed-runtime behaviour:** where the MiniZinc bundle lives, how to override it (`OPENCONSTRAINT_MCP_RUNTIME_DIR`), and what version it pins.
- **Surface third-party licenses** for anything bundled (MiniZinc, OR-Tools, Chuffed) in a `LICENSES/` directory or an equivalent README section.

## Definition of Done

A change is done when:

1. `just check` is green.
2. New behavior has unit tests; non-trivial behavior has at least one CLI- or MCP-level smoke test.
3. User-facing changes are reflected in `README.md`.
4. No new telemetry, no new hidden network calls, no new global mutable state.
