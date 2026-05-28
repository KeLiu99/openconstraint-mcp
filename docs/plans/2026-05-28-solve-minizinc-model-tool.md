# `solve_minizinc_model` MCP tool — Implementation Plan

> **Executor notes**
>
> - Drive task-by-task; tick `- [ ]` boxes as you go. Each task lists files, behavior, tests, and a verification command — write the code yourself from the behavior description, don't expect ready-to-paste snippets.
> - Preflight: run `just --list` to confirm recipes are present. Fall back to `uv run …` only if `just` is unavailable.
> - **Commits:** this plan does not prescribe commits. Commit at points that make sense to you (typically after each green task), follow the repo's plain-message convention, and re-run `just check` before any commit.
> - **Local-first invariant:** this plan adds the first *execution* tool. It must use the managed runtime resolved through `runtime.get_minizinc_binary()` — never a bare `$PATH` lookup. It must not call any LLM, must not introduce LangChain / LangGraph, must not add hidden network calls, and must not execute arbitrary Python or shell.
>
> **Project root:** `/home/bios8086/PycharmProjects/PythonProject/openconstraint-mcp`

---

## Context

`openconstraint-mcp` currently exposes two introspection MCP tools (`check_runtime`, `list_available_solvers`) and one MCP prompt (`solve_constraint_problem`). The prompt already tells the client's LLM to call `solve_minizinc_model` *if available*, with an explicit CLI-walkthrough fallback when it is not.

This plan implements that missing tool: the deterministic local executor that takes a complete MiniZinc model authored by the client LLM, runs it through the managed MiniZinc runtime, and returns enough structured signal for the LLM to either explain the result or revise the model and retry.

## Goal

A new MCP tool `solve_minizinc_model` that:

1. Accepts the LLM-drafted MiniZinc source plus an optional solver and an optional time limit.
2. Writes the model to a private temp file and invokes the managed `minizinc` binary against it with `--solver <solver> --time-limit <timeout_ms>`.
3. Returns a structured `SolveResult` Pydantic model containing **status**, **solver**, **stdout**, **stderr**, and **elapsed_ms**.
4. Distinguishes argument / environment problems (raise an MCP error — LLM cannot fix this call shape) from solving outcomes including MiniZinc-reported model errors (return structured result — LLM revises and retries).

## Non-goals

- `.dzn` files / external data parameters.
- User-provided file paths or multi-file MiniZinc projects.
- Parsing the solution into typed variable JSON. The LLM reads `stdout` from the model's `output` block — that is enough for v0 and avoids coupling to per-model output shape.
- Multiple-solution enumeration (`-n`, `-a`, intermediate solutions).
- Custom MiniZinc flags beyond `--solver` and `--time-limit`.
- A native OR-Tools backend. MiniZinc-only this PR; `cp-sat` is reached *through* MiniZinc's `--solver cp-sat`.
- A `solve` vs `optimize` split. The model's own `solve satisfy;` / `solve minimize …;` / `solve maximize …;` statement determines the problem type.
- Telemetry of any kind.
- Touching `runtime_install.py` or any download path — execution path is offline.

## Architecture

```
cli  ─►  server  ─►  minizinc  ─►  runtime  ─►  schemas
                       ▲
                       └── solve_model() lives here, alongside list_solvers()
```

- `solve_model` is a sibling of `list_solvers` in `minizinc.py`. Both are subprocess wrappers around the managed binary; they share the runtime-missing guard, the `subprocess.run` patch surface used in tests, and the `MiniZincExecutionError` type for genuine binary failures.
- `server.py` registers a new `@mcp.tool` named `solve_minizinc_model` that delegates straight to `minizinc.solve_model`, mapping environment-class failures (`RuntimeMissingError`, `MiniZincExecutionError`, `ValueError` for empty input) to an MCP `RuntimeError` — matching how `list_available_solvers` handles `list_solvers`.
- `schemas.py` gains a `SolveResult` model. No `SolveRequest` model — FastMCP passes args as keyword arguments and Pydantic-validates them per parameter annotation.
- Layering invariant is preserved: imports still flow left-to-right only; `runtime_install` remains untouched.

## Tech stack

- Python 3.12 target. `subprocess.run` (with `timeout=` and `capture_output=True`), `tempfile.TemporaryDirectory` for the scratch `.mzn` file, `time.monotonic` for `elapsed_ms`, `typing.Literal` for the closed `SolveStatus` value set, plus the existing `pydantic` / `mcp[cli]` / `typer` stack.
- No new dependencies. `pyproject.toml` is not modified.

## Safety & privacy invariants

- **Managed runtime only.** The subprocess command always starts with `str(runtime.get_minizinc_binary())`. Never `"minizinc"` from `$PATH`.
- **No server-side LLM call.** This tool runs MiniZinc and returns the bytes verbatim. It does not summarise, classify, or repair the model.
- **No I/O paths we initiate or expose to LLM-controlled values, beyond the temp `.mzn` we write.** `solve_model` writes exactly one file (the model body, into a private `TemporaryDirectory`) and invokes exactly one process (the managed `minizinc` binary). The tool does *not* accept a user-provided file path argument, does *not* support `.dzn` data files, and does *not* support multi-file projects in v0. MiniZinc itself will of course read its own bundled standard library (`<runtime_dir>/share/minizinc/std/*.mzn`), its solver configuration files (`.msc`), and any `include "...";` directives that appear inside the LLM-drafted model. Standard-library includes (`include "alldifferent.mzn";`, `include "globals.mzn";`, etc.) resolve against the bundled stdlib and are expected. Relative includes (`include "data.mzn";`) resolve against the directory containing the model file — which in our case is the private temp directory we just created, so they land on emptiness and surface as a normal MiniZinc compile error in `stderr` with `status="error"` rather than accidentally reading something from the user's working directory. To make that isolation explicit and prevent regressions, the `subprocess.run` call also sets `cwd=<temp_dir>` so the subprocess's working directory matches the model file's directory; any cwd-relative file lookup MiniZinc (or a future MiniZinc version) might perform therefore lands on the same empty temp dir rather than on the MCP server's working directory. We do **not** parse the model to whitelist includes — that would either re-implement MiniZinc's resolver or block legitimate stdlib use.
- **No network.** Neither the tool nor MiniZinc itself initiates network I/O on this code path; the only sanctioned network call in the package remains `install-runtime`.
- **No arbitrary code execution beyond MiniZinc itself.** The only external process invoked is the managed `minizinc` binary, with arguments we control (`--solver`, `--time-limit`, and the path to a temp file we just wrote). The model text is *not* shell-interpolated — it lands on disk as file contents.
- **Model contents stay in process / on temp disk.** The `.mzn` file lives inside a `TemporaryDirectory` and is deleted when the call returns. We do not log the model body; only the model's own `output`-block stdout flows back through the tool result.
- **Wall-clock cap on subprocess.** `subprocess.run(timeout=…)` is set slightly above `timeout_ms` so a misbehaving MiniZinc cannot hang the MCP server indefinitely; on `TimeoutExpired` the child is killed and `status="timeout"` is returned.

## File structure

| File | Action | Responsibility |
| ---- | ------ | -------------- |
| `src/openconstraint_mcp/schemas.py` | Modify | Add `SolveStatus` `Literal` alias and `SolveResult(BaseModel)`. |
| `src/openconstraint_mcp/minizinc.py` | Modify | Add `solve_model(model, *, solver, timeout_ms)`; reuse existing `RuntimeMissingError` / `MiniZincExecutionError`; add private `_parse_status` helper. |
| `src/openconstraint_mcp/server.py` | Modify | Register `@mcp.tool("solve_minizinc_model")` that calls `solve_model`, wraps environment-class errors into a single `RuntimeError` for the MCP client. |
| `tests/test_minizinc.py` | Modify | Add unit tests for `solve_model` (status parsing, command shape, default args, timeout, errors). |
| `tests/test_server.py` | Modify | Add MCP-level tests: tool is listed; happy-path call returns `SolveResult`; runtime-missing surfaces actionable error; MiniZinc model errors come back as `status="error"` (not as raised exceptions). |
| `README.md` | Modify | Document the new MCP tool, its arguments, its result schema, and how it relates to `solve_constraint_problem`. Drop the "not yet implemented" note from the v0 limitations section. |

`runtime.py`, `runtime_install.py`, `cli.py`, `conftest.py`, and `pyproject.toml` are not modified.

---

## Module-level interface (signatures only)

```python
# schemas.py additions
SolveStatus = Literal[
    "satisfied",          # at least one solution, search incomplete
    "optimal",            # search complete (optimum proven, or all solutions enumerated)
    "unsatisfiable",      # =====UNSATISFIABLE=====
    "unknown",            # =====UNKNOWN===== or no recognised marker on a clean exit
    "unbounded",          # =====UNBOUNDED=====
    "unsat_or_unbounded", # =====UNSATorUNBOUNDED=====
    "error",              # MiniZinc model/compile/solver error (=====ERROR===== or nonzero exit)
    "timeout",            # subprocess wall-clock cap fired
]

class SolveResult(BaseModel):
    status: SolveStatus
    solver: str          # echoed from request, so the LLM sees what actually ran
    stdout: str          # raw MiniZinc stdout (solution lines + separator markers)
    stderr: str          # raw MiniZinc stderr (compile/syntax/solver errors land here)
    elapsed_ms: int      # wall-clock duration of the subprocess call
```

```python
# minizinc.py additions
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000   # 30s — conservative default per spec
DEFAULT_SOLVER: str = "cp-sat"           # matches AGENTS.md / solve_constraint_problem prompt

def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult:
    # raises ValueError on empty/whitespace model or non-positive timeout_ms
    # raises RuntimeMissingError when the managed runtime is not installed
    # raises MiniZincExecutionError on OSError from the subprocess itself
    ...

# Private helper (named here because tests pin its behavior):
def _parse_status(stdout: str, returncode: int, timed_out: bool) -> SolveStatus: ...

# Private helper for normalising TimeoutExpired output (see "Key behavior decisions" #8):
def _coerce_to_text(payload: str | bytes | None) -> str: ...
```

```python
# server.py additions
@mcp.tool(description="Run a complete MiniZinc model through the managed runtime.")
def solve_minizinc_model(
    model: str,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult: ...
```

The MCP-tool function is intentionally a thin adapter: validate-and-delegate, plus the same `RuntimeError` wrapping pattern `list_available_solvers` already uses.

---

## Key behavior decisions

These pin the choices the executor must not silently re-derive:

1. **Solver string is passed through verbatim.** No mapping table from short names to long IDs. MiniZinc's `--solver` already accepts both forms, and if the user picks a solver MiniZinc doesn't know about, MiniZinc emits a clear error which lands in `stderr` with `status="error"`. The LLM can repair.
2. **Empty / whitespace-only model is a validation error, not a solve outcome.** `solve_model` raises `ValueError`; the server layer converts it to a `RuntimeError` whose message names "model must not be empty". This matches the established pattern: argument problems raise, solving outcomes return.
3. **Runtime-missing is a raise.** Same shape as `list_solvers`: raises `RuntimeMissingError`, surfaced at the server layer as a `RuntimeError("…install-runtime…")`. The LLM cannot recover by changing call shape; the *user* must install the runtime.
4. **MiniZinc model errors are a return, not a raise.** Nonzero exit code or `=====ERROR=====` marker → `SolveResult(status="error", stderr=<full text>, …)`. The full stderr is the most actionable signal we can hand the client LLM for revise-and-retry, and matches the prompt's `"If MiniZinc reports a syntax, type, or solver error, revise the model and retry"` instruction.
5. **Subprocess `OSError` (binary not executable mid-run, kernel `ENOEXEC`, etc.) raises `MiniZincExecutionError`.** This is "the binary itself is broken", same class as the failure surfaced by `list_solvers`; advising the user to reinstall the runtime is the right move.
6. **Subprocess wall-clock timeout is the user-supplied `timeout_ms` plus a small grace (e.g. +5000 ms).** MiniZinc itself honours `--time-limit` and gracefully emits `=====UNKNOWN=====`; the outer subprocess timeout exists only as a hard kill-switch in case the binary misbehaves and never returns. When the outer timeout fires, `status="timeout"` and whatever stdout/stderr was captured up to that point is returned.
7. **No `LD_LIBRARY_PATH` shim in this PR.** `list_solvers` does not set one today; if real-runtime smoke testing surfaces a shared-library issue, that fix belongs to a follow-up that touches both `list_solvers` and `solve_model` at once. Do not add it preemptively.
8. **`TimeoutExpired.stdout` / `.stderr` may be bytes even when we pass `text=True`.** Python's `subprocess` documents that the decoded-text guarantee applies to a *successful* return path; when `TimeoutExpired` is raised, the captured buffers carry whatever was in flight at kill time, which can be either `str` or `bytes` depending on where the read was interrupted. The `SolveResult` schema requires `str`, so the implementation must funnel both attributes through a `_coerce_to_text` helper that returns `""` for `None`, returns the payload unchanged when it is already `str`, and decodes `bytes` with `decode("utf-8", errors="replace")`. The test in Task 4 must use `output=b"partial"` (bytes, not str) so a future regression that drops the coercion fails loudly.
9. **`timeout_ms` must be positive.** A `timeout_ms <= 0` is meaningless for solving (MiniZinc's `--time-limit 0` is documented as "no limit", which silently subverts the wall-clock cap, and `subprocess.run(timeout=-1)` raises `ValueError` from inside the call rather than returning a clean `status`). Validate early — before the runtime check — and raise `ValueError("timeout_ms must be positive")`. The server layer converts that to a `RuntimeError` the same way it converts the empty-model `ValueError`. No tolerance for `0` — the LLM should pass a real budget or omit the argument entirely to get the default.

## Status parsing precedence

The order is fixed (highest priority first), because some markers can co-occur with `----------`:

1. `timed_out` flag → `"timeout"`.
2. `=====ERROR=====` in stdout → `"error"`.
3. `=====UNSATISFIABLE=====` → `"unsatisfiable"`.
4. `=====UNBOUNDED=====` → `"unbounded"`.
5. `=====UNSATorUNBOUNDED=====` → `"unsat_or_unbounded"`.
6. `=====UNKNOWN=====` → `"unknown"`.
7. `==========` present → `"optimal"` (search proven complete).
8. `----------` present → `"satisfied"` (at least one solution, search incomplete).
9. `returncode != 0` and none of the above → `"error"`.
10. Else → `"unknown"`.

`"satisfied"` is the "we found something but didn't prove optimality" status — typical for an optimization problem where MiniZinc hit `--time-limit` after finding a feasible solution. `"optimal"` covers both proven-optimal (optimization) and all-solutions-enumerated (satisfy) — both emit `==========`. The LLM can disambiguate from the model's own `solve` statement if it cares.

---

## Task list

Each task is TDD: write tests describing the behavior, run them red, implement, run them green, then verify with the listed command.

### Task 1: `SolveResult` schema

**Files:** modify `src/openconstraint_mcp/schemas.py`, modify `tests/test_minizinc.py` (or add a tiny `tests/test_schemas.py` if you prefer — the existing tests don't have a dedicated schema test file, so a smoke import in `test_minizinc.py` is acceptable).

- [ ] Add the `SolveStatus` `Literal` alias and the `SolveResult` model. Field set: `status`, `solver`, `stdout`, `stderr`, `elapsed_ms`. All required; no defaults (defaults belong on the *function*, not the result schema). `elapsed_ms` is `int`.
- [ ] Test: construct `SolveResult(...)` with valid args and assert the round-trip via `model_dump()`. Construct with an invalid status string and assert `ValidationError` (this pins the `Literal`).
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green (or whichever file you put the schema test in).

### Task 2: `_parse_status` helper

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior: pure function over `(stdout: str, returncode: int, timed_out: bool)` returning a `SolveStatus`. Implements the priority table in "Status parsing precedence" exactly.

Tests (parametrize over the precedence table):

- [ ] `timed_out=True` always wins, regardless of stdout.
- [ ] `=====ERROR=====` beats `=====UNSATISFIABLE=====` when both happen to appear (defensive — does not happen in practice but pins precedence).
- [ ] `==========` alone → `"optimal"`; `----------` alone → `"satisfied"`; both → `"optimal"` (optimal wins).
- [ ] `=====UNSATISFIABLE=====` / `=====UNBOUNDED=====` / `=====UNSATorUNBOUNDED=====` / `=====UNKNOWN=====` each map to their canonical status.
- [ ] Nonzero `returncode` with no recognised marker → `"error"`.
- [ ] Zero `returncode` and no marker → `"unknown"`.

Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 3: `solve_model` — happy path + command shape

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior:

- Validate input — both checks happen *before* the runtime check so argument bugs surface on a machine with no runtime installed too:
  - `model.strip() == ""` → raise `ValueError("model must not be empty")`.
  - `timeout_ms <= 0` → raise `ValueError("timeout_ms must be positive")`.
- Check runtime: if `not is_runtime_installed()` → raise `RuntimeMissingError` with the same "run `openconstraint-mcp install-runtime`" message shape `list_solvers` already uses.
- Resolve binary via `get_minizinc_binary()`.
- Open a `tempfile.TemporaryDirectory`; write `model` to `<tmp>/model.mzn`.
- Build command: `[str(binary), "--solver", solver, "--time-limit", str(timeout_ms), str(model_file)]`.
- Time the call with `time.monotonic()` around `subprocess.run(cmd, capture_output=True, text=True, timeout=(timeout_ms / 1000) + 5, cwd=<temp_dir>)`. Pass the same `TemporaryDirectory` path as `cwd=` so the subprocess's working directory is the model file's directory — this enforces the include-isolation invariant from the Safety section. Do **not** pass `check=True` — we want to inspect nonzero exits and return them as `status="error"`, not raise.
- Compute `elapsed_ms = int((time.monotonic() - start) * 1000)` (clamp to at least 0; on very fast subprocesses on coarse clocks, `0` is a fine value).
- Parse status via `_parse_status(stdout, returncode, timed_out=False)`.
- Return `SolveResult(status=…, solver=solver, stdout=…, stderr=…, elapsed_ms=…)`.

Tests (monkeypatch `openconstraint_mcp.minizinc.subprocess.run` exactly the way `test_list_solvers_parses_solvers_json` does — record the call args list so we can assert on it):

- [ ] **Happy satisfied:** mocked `subprocess.run` returns stdout `"x = 3;\n----------\n=========="` (so the test pins that `==========` makes `"optimal"`), rc=0, stderr=`""`. With `fake_minizinc_binary` fixture installed: `solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")` returns `SolveResult(status="optimal", solver="cp-sat", stdout=<that text>, stderr="", elapsed_ms=<int>)`.
- [ ] **Command shape:** the recorded call args satisfy:
  - First arg is `str(fake_minizinc_binary)` (not `"minizinc"`).
  - Contains `"--solver"` immediately followed by `"cp-sat"`.
  - Contains `"--time-limit"` immediately followed by `"30000"`.
  - Last arg is a path that ends with `.mzn`, exists at the moment `subprocess.run` is called (verified by having the mock `os.path.exists` the path), and contains the model string. The simplest implementation: the mock reads the file path off the call args and asserts its contents match.
  - The recorded `cwd=` kwarg equals the parent directory of the `.mzn` model file. This pins the include-isolation invariant from the Safety section — a regression that drops `cwd=` (and silently lets MiniZinc resolve cwd-relative paths against the MCP server's working directory) fails this assertion.
- [ ] **Custom solver:** `solve_model("solve satisfy;", solver="gecode")` → recorded `--solver gecode`; returned `solver="gecode"`.
- [ ] **Custom timeout:** `solve_model("solve satisfy;", timeout_ms=5000)` → recorded `--time-limit 5000`; outer `subprocess.run` timeout is `5 + 5 = 10` seconds (assert via inspecting kwargs).
- [ ] **Default args:** zero-arg call (only `model`) records `--solver cp-sat` and `--time-limit 30000`.

Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 4: `solve_model` — error paths

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior to round out (all should land in the function from Task 3 — this is more "make sure these cases are handled", not a fresh code path):

- `subprocess.TimeoutExpired` → return `SolveResult(status="timeout", solver=…, stdout=_coerce_to_text(e.stdout), stderr=_coerce_to_text(e.stderr), elapsed_ms=…)`. **Do not** raise; the wall-clock timeout is a normal outcome. Use the `_coerce_to_text` helper (defined in the interface block above) because `TimeoutExpired.stdout/stderr` can be `bytes` even when the call passed `text=True` — the decoded-text guarantee only applies to a successful return.
- `OSError` from `subprocess.run` (e.g. binary not executable mid-call) → raise `MiniZincExecutionError` whose message names the binary path and points at `install-runtime` for reinstallation (mirror the wording of `list_solvers`'s wrapper).
- MiniZinc nonzero exit with `=====ERROR=====` in stdout *or* compile error on stderr → `SolveResult(status="error", stdout=…, stderr=…, …)`. **Do not** raise.
- `=====UNSATISFIABLE=====` on stdout, rc=0 → `SolveResult(status="unsatisfiable", …)`.

Tests:

- [ ] **Empty model raises:** `pytest.raises(ValueError, match="empty")`. Subprocess is not invoked (use a sentinel mock that records the call; assert it was not called).
- [ ] **Whitespace-only model raises:** same as above with `"\n\n  \t\n"`.
- [ ] **Non-positive `timeout_ms` raises:** `pytest.raises(ValueError, match="positive")` for each of `timeout_ms=0` and `timeout_ms=-1`. Subprocess is not invoked.
- [ ] **`timeout_ms` validation precedes runtime check:** with `fake_runtime_dir` (no binary), call with `timeout_ms=0`. Expect `ValueError`, **not** `RuntimeMissingError` — argument bugs surface on machines without the runtime too.
- [ ] **Runtime missing raises:** with `fake_runtime_dir` (no binary), `pytest.raises(RuntimeMissingError)` whose message contains both `"install-runtime"` and `"MiniZinc"`.
- [ ] **MiniZinc compile error returns structured result:** mocked `subprocess.run` returns rc=1, stdout=`""`, stderr=`"MiniZinc: syntax error: ..."`. Assertion: returns `SolveResult(status="error", stderr=<contains "syntax error">, …)`; **no** raise.
- [ ] **Unsatisfiable returns structured result:** mocked stdout `"=====UNSATISFIABLE=====\n"`, rc=0. Returns `status="unsatisfiable"`.
- [ ] **TimeoutExpired with bytes payload returns structured result:** mocked `subprocess.run` raises `subprocess.TimeoutExpired(cmd=…, timeout=…, output=b"partial", stderr=b"")` — note `output=b"partial"`, **bytes**, not `str`. This pins the `_coerce_to_text` decoding; a regression that re-introduces `e.stdout or ""` would surface as `stdout=b"partial"` which fails the `SolveResult` schema. Returns `status="timeout"`, `stdout="partial"` (decoded), `stderr=""`. `elapsed_ms` is a non-negative int.
- [ ] **TimeoutExpired with None payloads returns structured result:** same fixture as above but `output=None, stderr=None` (simulating a kill before any read). `stdout` and `stderr` are both `""`; `status="timeout"`.
- [ ] **OSError raises `MiniZincExecutionError`:** mocked `subprocess.run` raises `OSError(8, "Exec format error")`. Raises `MiniZincExecutionError` whose message names `install-runtime`.

Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 5: MCP tool wiring

**Files:** modify `src/openconstraint_mcp/server.py`, modify `tests/test_server.py`.

Behavior:

- Register `@mcp.tool(description=...)` named `solve_minizinc_model`. The description should be short and concrete: it should mention "managed local MiniZinc runtime", "model is complete MiniZinc source", and that the returned object includes the solver's raw stdout/stderr so the caller can revise and retry on errors. **Do not** instruct the LLM how to draft the model — that lives in the `solve_constraint_problem` prompt.
- Parameters: `model: str`, `solver: str = DEFAULT_SOLVER`, `timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS`. Annotations matter — FastMCP derives the tool's input schema from them.
- Body: try `return solve_model(model, solver=solver, timeout_ms=timeout_ms)`; on `RuntimeMissingError` or `MiniZincExecutionError` re-raise as `RuntimeError(str(exc))` (the same pattern `list_available_solvers` uses); on `ValueError` re-raise as `RuntimeError(str(exc))` so the empty-model and non-positive-`timeout_ms` messages both reach the MCP client cleanly.
- Re-export `DEFAULT_SOLVER` and `DEFAULT_SOLVE_TIMEOUT_MS` from `minizinc` so `server.py` can reference them in the parameter defaults without duplicating literals.

Tests (extend `tests/test_server.py`):

- [ ] **Tool is listed:** `mcp.list_tools()` includes a tool named `solve_minizinc_model`. The tool's input schema contains `model`, `solver`, `timeout_ms`.
- [ ] **Happy path through FastMCP:** monkeypatch `openconstraint_mcp.minizinc.subprocess.run` to return rc=0 with a stdout containing `"----------\n=========="`. With `fake_minizinc_binary` installed, `await mcp.call_tool("solve_minizinc_model", {"model": "solve satisfy;"})` returns a structured result the FastMCP testing layer surfaces. Assert the returned content has `status == "optimal"` and `solver == "cp-sat"`.
- [ ] **Runtime missing surfaces actionable error:** with `fake_runtime_dir` (no binary), `await mcp.call_tool("solve_minizinc_model", {"model": "solve satisfy;"})` raises (the FastMCP testing API surfaces tool errors as exceptions, exactly like the existing `test_list_available_solvers_surfaces_actionable_error_on_binary_failure`). Message contains both `"install-runtime"` and `"MiniZinc"`.
- [ ] **Empty model surfaces actionable error:** call with `{"model": ""}`. Raises; message contains `"empty"`. Subprocess is not invoked.
- [ ] **Non-positive `timeout_ms` surfaces actionable error:** call with `{"model": "solve satisfy;", "timeout_ms": 0}`. Raises; message contains `"positive"`. Subprocess is not invoked.
- [ ] **MiniZinc model error is returned, not raised:** monkeypatch `subprocess.run` to return rc=1 with `stderr="MiniZinc: type error: undefined identifier 'xz'"`. Call the tool; the call does **not** raise; the returned structured content has `status == "error"` and `stderr` contains the type-error text. This is the critical contract for LLM revise-and-retry.

Verify: `just pytest tests/test_server.py -v` → green; then `just pytest -v` → all green.

### Task 6: README

**Files:** modify `README.md`.

- [ ] Under "MCP tools", add `solve_minizinc_model` to the list with its parameter set (`model: str`, `solver: str = "cp-sat"`, `timeout_ms: int = 30000`) and the returned schema (`status`, `solver`, `stdout`, `stderr`, `elapsed_ms`). Enumerate the possible `status` values once, in the same order as the precedence table, so the reader knows the closed set.
- [ ] Briefly note the division of labor: the `solve_constraint_problem` MCP prompt guides the client LLM to draft a model; `solve_minizinc_model` executes the drafted model locally and returns the runtime's verbatim output. Reinforce "LLM proposes, server verifies."
- [ ] State the failure-mode contract explicitly so MCP-client authors know what to handle: **environment/argument problems** (runtime not installed, empty model, non-positive `timeout_ms`, subprocess OS error) surface as MCP errors the client must show the user; **MiniZinc / solver outcomes** (unsat, unbounded, timeout, model error) come back as a normal structured result whose `status` field encodes the outcome, so a client LLM can branch on it without exception handling. Call out that `timeout_ms` must be strictly positive — `0` is not "no timeout", it is a validation error.
- [ ] Strike the "No `solve_minizinc_model` execution tool yet" bullet from the "v0 limitations" section — that limitation no longer holds. Leave the surrounding bullets (no telemetry, Linux x86_64 installer only, network only in `install-runtime`) untouched.
- [ ] Cross-check that the `solve_constraint_problem` prompt section in the README still reads correctly now that the referenced tool exists. Specifically, the "Call the future `solve_minizinc_model` tool if it is available, …" phrasing should drop "future" so the prose matches reality. Do not rewrite the rest of the section.

Verify: read the rendered README sections; no code to test.

### Task 7: Final `just check` + manual smoke test

- [ ] `just check` — lint + typecheck + tests green.
- [ ] Manual integration smoke (Linux x86_64 with the managed runtime installed — not part of `just check`):
  - Run the stdio server (`just run`) and from an MCP client (or via the MCP inspector), call `solve_minizinc_model` with a tiny model that does **not** require a stdlib include — for example `var 1..5: x; constraint x > 2; solve satisfy; output ["x=\(x)\n"];`. Confirm `status="optimal"` (or `"satisfied"`) and that `stdout` contains `x=`. (Avoid global constraints like `alldifferent(x)` here unless you also include `include "alldifferent.mzn";` — globals require their stdlib include and would otherwise fail with a compile error that masks whether *our* plumbing works.)
  - Then call with a model that *does* exercise a stdlib include — `include "alldifferent.mzn"; array[1..3] of var 1..3: x; constraint alldifferent(x); solve satisfy; output ["x=\(x)\n"];` — to confirm stdlib resolution works through the managed runtime.
  - Call with an intentionally broken model (e.g. `solv satisfy;`). Confirm the call returns `status="error"` with the MiniZinc parse error in `stderr` — **not** an MCP error. This is the key acceptance signal for LLM revise-and-retry.
  - Call with `solver="gecode"` to confirm the solver argument is honoured.
  - Call with `timeout_ms=0` and confirm an MCP error mentioning `"positive"` (validation surfaces before any subprocess work).
- [ ] If the real-runtime smoke surfaces a shared-library load failure, add a minimal `env=` override that prepends `<runtime_dir>/lib` to `LD_LIBRARY_PATH` in **both** `list_solvers` and `solve_model` simultaneously. Do not add this shim preemptively — evidence-driven only.

## Acceptance criteria

- `solve_minizinc_model` appears in `mcp.list_tools()` with parameters `model`, `solver`, `timeout_ms`, and returns a `SolveResult` shape.
- Unit tests cover happy path, every status code in the `SolveStatus` literal set, the empty-model raise, the non-positive-`timeout_ms` raise, the runtime-missing raise, the MiniZinc-error structured return, the subprocess timeout structured return (with `output=b"…"` bytes payload exercising `_coerce_to_text`), and the OSError raise. None of them require a real MiniZinc binary.
- The subprocess command uses `runtime.get_minizinc_binary()` as the first argument, and `subprocess.run(...)` is invoked with `cwd=<temp_dir>` so the subprocess's working directory matches the model file's directory. A regression test pins both — `--solver` / `--time-limit` values flow through verbatim from the tool arguments, and the recorded `cwd=` matches the model file's parent directory.
- Environment / argument problems (runtime missing, empty model, non-positive `timeout_ms`, OSError on the binary) surface as `RuntimeError` to the MCP client. Solving outcomes including MiniZinc model errors surface as `SolveResult` with the appropriate status — the client LLM can revise the model and retry without parsing error messages.
- README documents the new MCP tool, its arguments, its result schema, and its relationship to `solve_constraint_problem`. The "no `solve_minizinc_model` yet" v0 limitation is removed.
- `just check` is green.
- No telemetry. No new network calls. No LangChain / LangGraph. No bare-`$PATH` MiniZinc invocation. No server-side LLM. No arbitrary code execution beyond the managed `minizinc` binary itself.

## Known risks

- **`==========` semantics depend on the model's `solve` statement.** For `solve satisfy;`, `==========` means "search complete; the printed solutions are all there is". For `solve minimize …;` it means "the last printed solution is provably optimal". Calling both of these `"optimal"` is a deliberate simplification — the LLM can disambiguate from the model body. If users complain, splitting into `"all_solutions"` and `"optimal"` is a small additive change to the `Literal` and `_parse_status`.
- **Default 30s timeout may be too short for non-trivial models.** This is the "conservative default" the spec called for; the LLM can pass `timeout_ms` explicitly when the problem warrants. Watch for feedback before raising it.
- **MiniZinc's stderr can be large on big models with bad constraints.** Returning it verbatim in `SolveResult` could blow up an MCP frame on pathological inputs. v0 returns it as-is; if it becomes a problem, truncate-with-marker in a follow-up rather than swallowing.
- **MiniZinc 2.9.7's exact marker spellings.** The precedence table reflects the documented markers; the real-runtime smoke in Task 7 is the first place a spelling drift would surface. If the bundled MiniZinc emits a variant not in the table, the parser falls through to `"unknown"` (with the raw stdout still visible to the LLM) — degraded but not silently wrong.
