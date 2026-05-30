# `check_minizinc_model` MCP tool — Implementation Plan

> **Executor notes**
>
> - Drive task-by-task; tick `- [ ]` boxes as you go. Each task lists files, behavior, tests, and a verification command — write the code yourself from the behavior description, don't expect ready-to-paste snippets.
> - Preflight: run `just --list` to confirm recipes are present. Fall back to `uv run …` only if `just` is unavailable.
> - **Commits:** this plan does not prescribe commits. Commit at points that make sense to you (typically after each green task), follow the repo's plain-message convention, and re-run `just check` before any commit.
> - **Local-first invariant:** this tool runs MiniZinc through the managed runtime resolved by `runtime.get_minizinc_binary()` — never a bare `$PATH` lookup. It must not call any LLM, must not introduce LangChain / LangGraph, must not add hidden network calls, must not accept user-provided file paths, and must not execute arbitrary Python or shell.
>
> **Project root:** `/home/bios8086/PycharmProjects/PythonProject/openconstraint-mcp`

---

## Context

`openconstraint-mcp` exposes two introspection tools (`check_runtime`, `list_available_solvers`), one execution tool (`solve_minizinc_model`), and one prompt (`solve_constraint_problem`). LLM-drafted MiniZinc frequently fails on syntax, type, missing-include, invalid-domain, or unsupported-construct errors *before* a solve is even meaningful. Today the only way for the client LLM to learn that is to spend a full solve attempt and read the error out of `SolveResult.stderr`.

This plan adds a lightweight **validate-before-solve** tool, `check_minizinc_model`, that compiles the model without running the search and returns structured diagnostics. The intended loop becomes **draft → check → repair → solve → explain**.

## Goal

A new MCP tool `check_minizinc_model` that:

1. Accepts complete LLM-drafted MiniZinc source plus an optional solver and optional time limit (same argument surface as `solve_minizinc_model`).
2. Runs the managed `minizinc` binary in **dry-run compile** mode (`-c, --compile`): it flattens the model to FlatZinc for the chosen solver but stops before search.
3. Returns a structured `CheckResult` with **status** (`"ok" | "error" | "timeout"`), **solver**, **stdout**, **stderr**, **elapsed_ms**.
4. Distinguishes argument/environment problems (raise an MCP error) from model diagnostics (return a structured `status="error"` result so the LLM can revise and retry).

The `solve_constraint_problem` prompt and `README.md` are updated so the recommended loop runs through `check_minizinc_model` before `solve_minizinc_model`.

## Why `-c` (decided)

`-c` is solver-aware and catches the full failure set the spec names — syntax, type, missing includes, invalid domains, **and** constructs the chosen solver cannot handle — because it performs the real flatten. The lighter `-e, --model-check-only` never flattens, so it misses solver-specific and flatten-time errors and would make the `solver` argument a no-op. `-c` is exactly the "dry-run compilation" AGENTS.md names as an intended feature.

Empirically confirmed against the bundled runtime: a valid model → `rc=0`, clean stdout (the generated `.fzn`/`.ozn` land in cwd); a syntax error and a type error each → `rc=1` with the diagnostic on **stderr**. Status therefore keys off the return code, not stdout markers.

## Non-goals

- `.dzn` files / external data parameters; user-provided file paths; multi-file MiniZinc projects.
- Parsing diagnostics into typed fields — `stderr` is returned verbatim, the most actionable signal for LLM revise-and-retry.
- A satisfiability verdict. `status="ok"` means *the model compiles*, **not** that it has a solution; a compilable model can still be unsatisfiable (detected only at solve time).
- A `--model-check-only` "light" mode, a `--instance-check-only` mode, or any check-mode selector argument. One mode (`-c`) this PR.
- A native OR-Tools backend; telemetry of any kind; touching `runtime_install.py` or any download path.

## Architecture

```
cli  ─►  server  ─►  minizinc  ─►  runtime  ─►  schemas
                       ▲
                       └── check_model() joins solve_model() here, both on a
                           shared _run_managed_minizinc() helper
```

- `check_model` is a sibling of `solve_model` in `minizinc.py`. The identical "validate args → check runtime → resolve managed binary → write temp `.mzn` → run subprocess with the wall-clock grace, handling `TimeoutExpired`/`OSError`" sequence is extracted into a private `_run_managed_minizinc` helper that **both** functions call. `solve_model`'s observable behavior is unchanged; its existing test suite (command shape, cwd, encoding, timeout grace, error paths) guards the refactor.
- The two callers differ only in (a) the extra command argument (`solve_model` adds nothing; `check_model` adds `-c`) and (b) how they map the run outcome to a result (`solve_model` → `_parse_status` → `SolveResult`; `check_model` → return-code → `CheckResult`).
- `server.py` registers a new `@mcp.tool` named `check_minizinc_model` that delegates to `check_model`, mapping environment-class failures (`RuntimeMissingError`, `MiniZincExecutionError`, `ValueError`) to a single `RuntimeError` — the same pattern `solve_minizinc_model` already uses.
- `schemas.py` gains `CheckStatus` and `CheckResult`. Layering invariant preserved: imports still flow left-to-right; `runtime_install` untouched.

## Tech stack

- Python 3.12 target. Reuses the existing `subprocess.run` / `tempfile.TemporaryDirectory` / `time.monotonic` / `typing.Literal` machinery already in `minizinc.py`. The internal run-outcome carrier is a `typing.NamedTuple` (ephemeral internal value, not a tool-result schema — Pydantic is reserved for the MCP-facing `CheckResult`).
- No new dependencies. `pyproject.toml` is not modified.

## Safety & privacy invariants

These are inherited unchanged from `solve_model` and re-asserted because `check_model` shares the subprocess path:

- **Managed runtime only.** The command always starts with `str(runtime.get_minizinc_binary())`. Never `"minizinc"` from `$PATH`.
- **No server-side LLM call.** The tool compiles and returns bytes verbatim; it does not summarise, classify, or repair the model.
- **Include / cwd isolation.** Exactly one file is written (the model body) into a private `TemporaryDirectory`, and `subprocess.run(cwd=<temp_dir>)` pins the working directory to that empty temp dir, so any cwd-relative `include "data.mzn";` resolves onto emptiness and surfaces as a normal compile error in `stderr` rather than reading from the server's working directory. Stdlib includes (`include "globals.mzn";`) resolve against the bundled stdlib and are expected.
- **Compile output is contained.** `-c` writes `model.fzn` / `model.ozn` into the same `TemporaryDirectory`, which is deleted when the call returns; nothing is read back, and stdout is not polluted with FlatZinc.
- **No network calls initiated by openconstraint-mcp.** This code path opens no sockets and makes no outbound requests; the only sanctioned network call anywhere in the package remains the user-invoked `install-runtime` download. We invoke MiniZinc and the bundled solvers as local subprocesses with no remote-endpoint or network-related flags. This is a statement about what *we* initiate and how we invoke the binary — not a kernel-enforced guarantee about the binary's internals, since we do not sandbox the child process.
- **No arbitrary code execution beyond the managed `minizinc` binary.** The only external process invoked is that binary, with arguments we control (`--solver`, `--time-limit`, `-c`, and the temp model path); the model text lands on disk as file contents and is never shell-interpolated.
- **Wall-clock cap.** `subprocess.run(timeout = timeout_ms/1000 + 5)` is the hard kill-switch; on `TimeoutExpired` the child is killed and `status="timeout"` is returned with whatever was captured.

## File structure

| File | Action | Responsibility |
| ---- | ------ | -------------- |
| `src/openconstraint_mcp/schemas.py` | Modify | Add `CheckStatus` `Literal` alias and `CheckResult(BaseModel)`. |
| `src/openconstraint_mcp/minizinc.py` | Modify | Add `_RunOutcome` NamedTuple + `_run_managed_minizinc` helper; refactor `solve_model` onto it (behavior-preserving); add `check_model(model, *, solver, timeout_ms)`. |
| `src/openconstraint_mcp/server.py` | Modify | Register `@mcp.tool("check_minizinc_model")`; import `check_model` / `CheckResult`; add a minimal validate-before-solve step to the `solve_constraint_problem` prompt. |
| `tests/test_minizinc.py` | Modify | Add `check_model` unit tests (command shape incl. `-c` and managed binary, `ok`, `error`, empty/whitespace, non-positive timeout, runtime-missing, timeout, OSError, custom solver) and `CheckResult` round-trip / unknown-status. |
| `tests/test_server.py` | Modify | Add MCP-level tests: tool listed with `{model, solver, timeout_ms}`; happy `ok`; compile error returned as structured `status="error"` (not raised); runtime-missing → MCP error; empty model → MCP error before subprocess; prompt mentions `check_minizinc_model` and orders it before solving; local-first boundary preserved. |
| `README.md` | Modify | Document the new tool, its args, its `CheckResult` schema, the `-c`/"compiles ≠ satisfiable" semantics, and the recommended draft → check → repair → solve → explain loop; update the tool-count sentence and the prompt step list. |

`runtime.py`, `runtime_install.py`, `cli.py`, `conftest.py`, and `pyproject.toml` are not modified.

---

## Module-level interface (signatures only)

```python
# schemas.py additions
CheckStatus = Literal[
    "ok",       # rc == 0 — the model compiled (flattened) for the chosen solver
    "error",    # rc != 0 — syntax/type/include/domain/unsupported-construct error (see stderr)
    "timeout",  # subprocess wall-clock cap fired during compilation
]

class CheckResult(BaseModel):
    status: CheckStatus
    solver: str          # echoed from request: the solver the model was flattened for
    stdout: str          # raw MiniZinc stdout (normally empty on a clean compile)
    stderr: str          # raw MiniZinc stderr (diagnostics + warnings land here)
    elapsed_ms: int      # wall-clock duration of the subprocess call
```

```python
# minizinc.py additions
class _RunOutcome(NamedTuple):
    timed_out: bool
    returncode: int      # meaningful only when timed_out is False
    stdout: str
    stderr: str
    elapsed_ms: int

def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],   # () for solve, ("-c",) for check
) -> _RunOutcome:
    # raises ValueError on empty/whitespace model or non-positive timeout_ms
    # raises RuntimeMissingError when the managed runtime is not installed
    # raises MiniZincExecutionError on OSError from the subprocess itself
    # returns _RunOutcome(timed_out=True, ...) on subprocess.TimeoutExpired
    ...

def check_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> CheckResult: ...
```

```python
# server.py additions
@mcp.tool(description="Compile-check a complete MiniZinc model through the managed runtime, without solving.")
def check_minizinc_model(
    model: str,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> CheckResult: ...
```

`_run_managed_minizinc` builds the command as
`[str(binary), "--solver", solver, "--time-limit", str(timeout_ms), *extra_args, str(model_file)]`
so the model file stays the **last** argument (the existing `solve_model` command-shape test reads `cmd[-1]`), and `solve_model`'s command is byte-identical to today's when `extra_args=()`.

## Key behavior decisions

These pin choices the executor must not silently re-derive:

1. **Status is return-code driven.** `rc == 0 → "ok"`; `rc != 0 → "error"`. There are no FlatZinc solve markers in compile output, so `_parse_status` is **not** reused. Timeout is handled by the `_run_managed_minizinc` `TimeoutExpired` branch → `status="timeout"`.
2. **`-c` flattens for the chosen solver.** The `solver` argument is meaningful and is threaded through verbatim, exactly as in `solve_model`. A construct one solver rejects may compile for another — that is intended; the LLM should check against the solver it will solve with.
3. **Compile diagnostics are a return, not a raise.** `rc != 0` (syntax/type/include/domain/unsupported) → `CheckResult(status="error", stderr=<full text>, …)`. The full stderr is the repair signal for the client LLM. Mirrors `solve_minizinc_model`'s contract for model errors.
4. **Argument/environment problems raise.** Empty/whitespace model → `ValueError("model must not be empty")`; non-positive `timeout_ms` → `ValueError("timeout_ms must be positive")`; runtime missing → `RuntimeMissingError`; subprocess `OSError` → `MiniZincExecutionError`. The server layer converts the first three classes to `RuntimeError(str(exc))` for the MCP client. Same precedence as `solve_model`: argument checks run **before** the runtime check.
5. **Warnings keep `status="ok"`.** The MiniZinc flattener may emit warnings (e.g. "model inconsistency detected") to stderr while still exiting `0`. That stays `"ok"` with non-empty `stderr` — the LLM sees the warning. We do not promote warnings to `"error"`; `"ok"` means "compiled", not "warning-free".
6. **`"ok"` is not "satisfiable".** Compilation does not run search. Document this so neither the README reader nor the prompt implies a clean check guarantees a solution exists.
7. **No extra flags.** Command is `--solver … --time-limit … -c <model>`. We do **not** add `--no-output-ozn` or redirect the `.fzn`; the generated files are contained in the temp dir and never read. Keep the command minimal and parallel to `solve_model`.
8. **Refactor is behavior-preserving.** Extracting `_run_managed_minizinc` must not change any observable behavior of `solve_model`. The full existing `tests/test_minizinc.py` + `tests/test_server.py` suites are the regression guard: run them green before refactoring and green after.

---

## Task list

Each task is TDD: write tests describing the behavior, run them red, implement, run them green, then verify with the listed command.

### Task 1: `CheckResult` schema

**Files:** modify `src/openconstraint_mcp/schemas.py`, modify `tests/test_minizinc.py`.

- [ ] Add the `CheckStatus` `Literal` alias (`"ok" | "error" | "timeout"`) and the `CheckResult` model with fields `status`, `solver`, `stdout`, `stderr`, `elapsed_ms` — all required, no defaults, `elapsed_ms: int`. Mirror `SolveResult`'s shape so MCP clients see a consistent envelope.
- [ ] Test: construct a valid `CheckResult` and assert the `model_dump()` round-trip; construct with an invalid `status` string and assert `ValidationError` (pins the `Literal`).
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 2: Extract `_run_managed_minizinc`; refactor `solve_model` onto it

**Files:** modify `src/openconstraint_mcp/minizinc.py`.

Behavior:

- [ ] Add the `_RunOutcome` NamedTuple and `_run_managed_minizinc(model, *, solver, timeout_ms, extra_args)` helper containing the exact sequence currently inside `solve_model`: empty-model `ValueError`, non-positive-`timeout_ms` `ValueError`, runtime-missing `RuntimeMissingError`, `get_minizinc_binary()`, `TemporaryDirectory` + write `model.mzn` (UTF-8), build the command with `*extra_args` before the model path, time `subprocess.run(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_ms/1000+5, cwd=<temp_dir>)`, `TimeoutExpired` → `_RunOutcome(timed_out=True, …)` (coerce payloads with the existing `_coerce_to_text`), `OSError` → `MiniZincExecutionError`.
- [ ] Reduce `solve_model` to: call the helper with `extra_args=()`; if `timed_out`, return `SolveResult(status="timeout", …)`; else `status = _parse_status(outcome.stdout, outcome.returncode, timed_out=False)` and return `SolveResult(…)`.
- [ ] This task adds **no new tests** — the existing `solve_model` suite is the contract. Verify nothing regressed.
- [ ] Verify: `just pytest tests/test_minizinc.py tests/test_server.py -v` → all existing tests green (command shape, cwd, encoding, defaults, custom solver/timeout, empty/whitespace, non-positive timeout, runtime-missing, compile error, unsatisfiable, timeout bytes/None, OSError).

### Task 3: `check_model` — happy path + command shape

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior: call `_run_managed_minizinc(model, solver=solver, timeout_ms=timeout_ms, extra_args=("-c",))`; if `timed_out` → `CheckResult(status="timeout", …)`; else `status = "ok" if outcome.returncode == 0 else "error"` → `CheckResult(…)`.

Tests (monkeypatch `openconstraint_mcp.minizinc.subprocess.run`, recording call args the way the existing `_record_subprocess` helper does):

- [ ] **Happy ok:** mocked `subprocess.run` returns `rc=0`, `stdout=""`, `stderr=""`; with `fake_minizinc_binary`, `check_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")` returns `CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=<int ≥ 0>)`.
- [ ] **Command shape:** recorded args satisfy — `cmd[0] == str(fake_minizinc_binary)` (not `"minizinc"`); `"--solver"` immediately followed by `"cp-sat"`; `"--time-limit"` immediately followed by `"30000"`; `"-c"` present; `cmd[-1]` ends with `.mzn`, existed at call time, and contains the model string; recorded `cwd` equals the model file's parent directory. This pins both the managed-binary invariant and include isolation.
- [ ] **Custom solver:** `check_model("solve satisfy;", solver="gecode")` → recorded `--solver gecode`; returned `solver="gecode"`.
- [ ] **Custom timeout:** `check_model("solve satisfy;", timeout_ms=5000)` → recorded `--time-limit 5000`; outer `subprocess.run` `timeout` kwarg is `10.0`.
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 4: `check_model` — error / timeout / environment paths

**Files:** modify `src/openconstraint_mcp/minizinc.py` (cases already handled by the helper from Task 2; this task pins them for `check_model`), modify `tests/test_minizinc.py`.

Tests:

- [ ] **Compile error returns structured result:** mocked `subprocess.run` returns `rc=1`, `stdout=""`, `stderr="Error: type error: …"`. `check_model(...)` returns `CheckResult(status="error", stderr=<contains "type error">, …)` — **no** raise.
- [ ] **Empty / whitespace model raises:** `pytest.raises(ValueError, match="empty")` for `""` and `"\n\n  \t\n"`; subprocess is **not** invoked (sentinel mock asserts not called).
- [ ] **Non-positive `timeout_ms` raises:** `pytest.raises(ValueError, match="positive")` for `0` and `-1`; subprocess not invoked.
- [ ] **`timeout_ms` validation precedes runtime check:** with `fake_runtime_dir` (no binary) and `timeout_ms=0`, expect `ValueError`, not `RuntimeMissingError`.
- [ ] **Runtime missing raises:** with `fake_runtime_dir`, `pytest.raises(RuntimeMissingError)` whose message contains `"install-runtime"` and `"MiniZinc"`.
- [ ] **Timeout returns structured result:** mocked `subprocess.run` raises `subprocess.TimeoutExpired(cmd=…, timeout=…, output=b"partial", stderr=b"")` (bytes payload, exercising `_coerce_to_text`). Returns `status="timeout"`, `stdout="partial"`, `stderr=""`, `elapsed_ms ≥ 0`.
- [ ] **OSError raises `MiniZincExecutionError`:** mocked `subprocess.run` raises `OSError(8, "Exec format error")`; message names `install-runtime`.
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 5: `check_minizinc_model` MCP tool wiring

**Files:** modify `src/openconstraint_mcp/server.py`, modify `tests/test_server.py`.

Behavior:

- [ ] Import `check_model` and `CheckResult`. Register `@mcp.tool(description=…)` named `check_minizinc_model` with params `model: str`, `solver: str = DEFAULT_SOLVER`, `timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS`. Description: short and concrete — "compile-checks a complete MiniZinc model through the managed local runtime without solving; returns structured diagnostics (status + raw stdout/stderr) so the caller can repair the model before calling `solve_minizinc_model`." Do **not** instruct the LLM how to draft the model — that lives in the prompt.
- [ ] Body: `try: return check_model(model, solver=solver, timeout_ms=timeout_ms)` except `(RuntimeMissingError, MiniZincExecutionError, ValueError) as exc: raise RuntimeError(str(exc)) from exc`.

Tests (extend `tests/test_server.py`, reuse the `_structured` and `_FakeCompletedProcess` helpers):

- [ ] **Tool is listed:** `mcp.list_tools()` includes `check_minizinc_model`; its input schema contains `model`, `solver`, `timeout_ms`.
- [ ] **Happy ok through FastMCP:** monkeypatch `subprocess.run` → `rc=0`, empty stdout/stderr; with `fake_minizinc_binary`, `await mcp.call_tool("check_minizinc_model", {"model": "solve satisfy;"})` → structured `status == "ok"`, `solver == "cp-sat"`.
- [ ] **Compile error is returned, not raised:** monkeypatch `subprocess.run` → `rc=1`, `stderr="Error: type error: undefined identifier 'xz'"`. Tool call does **not** raise; structured `status == "error"` and `stderr` contains the type-error text. Critical contract for revise-and-retry.
- [ ] **Runtime missing surfaces actionable error:** with `fake_runtime_dir`, the call raises; message contains `"install-runtime"` and `"MiniZinc"`.
- [ ] **Empty model surfaces actionable error:** `{"model": ""}` raises; message contains `"empty"`; subprocess not invoked.
- [ ] Verify: `just pytest tests/test_server.py -v` → green.

### Task 6: `solve_constraint_problem` prompt — minimal validate-before-solve step

**Files:** modify `src/openconstraint_mcp/server.py` (the `_SOLVE_CONSTRAINT_PROBLEM_PROMPT` string), modify `tests/test_server.py`.

Behavior — keep the edit minimal and focused; do **not** rewrite the prompt:

- [ ] Insert a concise validation step between drafting (current step 3) and executing (current step 4): instruct the LLM that, when `check_minizinc_model` is available, it must call it on the drafted model **first** and branch on the returned `status` — never call `solve_minizinc_model` ahead of a clean check:
  - `"ok"` → proceed to `solve_minizinc_model`. Solving is gated on this; do not solve until the check returns `"ok"`.
  - `"error"` → read `stderr`, repair the model, and re-run `check_minizinc_model`; loop until it returns `"ok"`. Do not solve while errors remain.
  - `"timeout"` → do **not** automatically solve. Explain that *validation* (not the solve) timed out, and ask the user how to proceed — simplify the model, raise `timeout_ms`, or try solving anyway — then continue only per the user's choice.
  State the recommended happy-path loop once as a **single recognizable line that names both tools in order** — e.g. `draft -> check_minizinc_model -> repair -> solve_minizinc_model -> explain` — so the ordering test (below) has a stable anchor instead of relying on whole-prompt offsets. Guard the tool reference with "if available", parallel to how the existing step 4 guards `solve_minizinc_model`; do not expand the CLI-walkthrough fallback.
- [ ] Do not introduce any server-side-LLM or LangChain/LangGraph phrasing; do not weaken the existing boundary reminders.

Tests:

- [ ] Add an assertion (extend the existing `test_solve_constraint_problem_prompt_guides_minizinc_drafting` substring list, or add a focused test) that the rendered prompt text contains `check_minizinc_model`.
- [ ] Add a test that pins the recommended order **scoped to that loop line**, not globally. A whole-prompt `index("check_minizinc_model") < index("solve_minizinc_model")` is fragile: the execute step and CLI-walkthrough already name `solve_minizinc_model`, and the intro could too, so a global first-index comparison can pass or fail for the wrong reasons. Instead, locate the single recommended-loop line written above (the one line containing **both** tool names), assert it is present, and assert that **within that line** `check_minizinc_model` precedes `solve_minizinc_model`.
- [ ] Add a test pinning the **timeout branch**: assert the rendered prompt tells the LLM not to auto-solve on a `"timeout"` check and to ask the user instead, anchored on stable keywords rather than exact prose — require `timeout_ms` (the raise-budget option), `simplify`, and a try-anyway cue (e.g. `anyway`) all to appear in the prompt text. This guards against the branch silently regressing back to "treat timeout as ok".
- [ ] Confirm the existing `test_solve_constraint_problem_prompt_preserves_local_first_boundary` still passes unchanged (no `LangChain`/`LangGraph`/server-side-LLM phrasing introduced).
- [ ] Verify: `just pytest tests/test_server.py -v` → green.

### Task 7: README

**Files:** modify `README.md`.

- [ ] Update the "## MCP tools" intro sentence so the tool count is accurate (now two introspection tools plus a model-check tool and a solve tool).
- [ ] Add a `check_minizinc_model` entry near `solve_minizinc_model`: parameters (`model: str`, `solver: str = "cp-sat"`, `timeout_ms: int = 30000`) and the `CheckResult` schema (`status: "ok" | "error" | "timeout"`, `solver`, `stdout`, `stderr`, `elapsed_ms`). State plainly: it runs MiniZinc's dry-run compile (`-c`) for the chosen solver and stops before search, so it catches syntax, type, missing-include, invalid-domain, and unsupported-construct errors; `"ok"` means **the model compiles, not that it is satisfiable**.
- [ ] State the failure-mode contract, parallel to `solve_minizinc_model`: environment/argument problems (runtime missing, empty `model`, non-positive `timeout_ms`, OS exec failure) → MCP errors; compile diagnostics → a normal `CheckResult` with `status="error"` and the diagnostic in `stderr`.
- [ ] Document the recommended loop — **draft → check → repair → solve → explain** — and note `check_minizinc_model` is the cheap pre-flight before `solve_minizinc_model`.
- [ ] Update the `solve_constraint_problem` prompt section's step list (around the current step 4) to mention the gated check-before-solve loop — solve only after a `"ok"` check, repair-and-recheck on `"error"`, and on `"timeout"` ask the user (simplify / raise `timeout_ms` / solve anyway) rather than auto-solving — matching the prompt change from Task 6. Do not rewrite the rest of the section.
- [ ] Verify: read the rendered README sections; no code to test.

### Task 8: Final `just check` + manual smoke test

- [ ] `just check` — lint + typecheck + tests green.
- [ ] Manual integration smoke (Linux x86_64 with the managed runtime installed — not part of `just check`): from an MCP client (or the MCP inspector),
  - call `check_minizinc_model` with a valid model (`var 1..5: x; constraint x > 2; solve satisfy; output ["x=\(x)\n"];`) → expect `status="ok"`, empty `stderr`;
  - call with a syntax error (`solv satisfy;`) → expect `status="error"` with the parse error in `stderr`, **not** an MCP error;
  - call with a type error (`var 1..3: x; constraint x + "a" == 2; solve satisfy;`) → expect `status="error"`;
  - call with `solver="gecode"` to confirm the solver flows through;
  - call with `timeout_ms=0` → MCP error mentioning `"positive"`.

## Acceptance criteria

- `check_minizinc_model` appears in `mcp.list_tools()` with parameters `model`, `solver`, `timeout_ms`, and returns a `CheckResult` shape (`status` ∈ `{"ok","error","timeout"}`).
- The compile command uses `runtime.get_minizinc_binary()` as `cmd[0]`, includes `-c`, threads `--solver`/`--time-limit` through verbatim, places the model file last, and runs with `cwd=<temp_dir>`; a regression test pins all of these.
- Unit tests cover happy `ok`, structured `error`, empty/whitespace raise, non-positive-`timeout_ms` raise, runtime-missing raise, `timeout` (bytes payload), and `OSError` raise — none require a real MiniZinc binary.
- Environment/argument problems surface as `RuntimeError` to the MCP client; compile diagnostics surface as `CheckResult(status="error")` so the LLM can repair without exception handling.
- `solve_model` behavior is unchanged: the full pre-existing suite passes after the `_run_managed_minizinc` refactor.
- The `solve_constraint_problem` prompt mentions `check_minizinc_model`, gates `solve_minizinc_model` on a `"ok"` check, loops repair-and-recheck on `"error"`, and on `"timeout"` stops and asks the user (simplify / raise `timeout_ms` / solve anyway) rather than auto-solving; it introduces check before solve and preserves the local-first / no-server-side-LLM boundary. Tests pin the tool mention, the loop-line ordering, and the timeout branch.
- README documents the new tool, its `CheckResult` schema, the `-c`/"compiles ≠ satisfiable" semantics, and the draft → check → repair → solve → explain loop.
- `just check` is green. No telemetry, no new network calls, no LangChain/LangGraph, no bare-`$PATH` MiniZinc, no server-side LLM, no arbitrary code execution beyond the managed binary, no user-provided file path arguments.

## Known risks

- **`-c` is solver-specific.** A model that compiles for `cp-sat` may not for another backend, and vice versa. Intended — the check is meaningful only against the solver the LLM intends to solve with. Documented in the README entry.
- **`"ok"` ≠ satisfiable.** Compilation skips search, so a clean check does not guarantee a solution exists; the flattener detects only trivial inconsistencies (often as an `rc=0` warning). Called out in the README and kept out of the prompt's wording.
- **Behavior-preserving refactor of a tested function.** Extracting `_run_managed_minizinc` touches `solve_model`. Mitigated by treating the existing suite as the contract (Task 2) and re-running it before/after; the command-shape test in particular guards arg order and `cwd`.
- **Marginal extra cost vs. a parse-only check.** `-c` flattens, which is heavier than `-e`. Accepted in exchange for catching solver-specific and flatten-time errors; the `--time-limit` + subprocess grace bound pathological flattens, surfacing as `status="timeout"`.
