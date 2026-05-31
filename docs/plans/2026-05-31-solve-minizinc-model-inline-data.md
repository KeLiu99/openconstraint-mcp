# Inline data for `solve_minizinc_model`, `check_minizinc_model`, `find_unsat_core` — Implementation Plan

> **Executor notes**
>
> - Drive task-by-task; tick `- [ ]` boxes as you go. Each task lists files, behavior, tests, and a verification command — write the code yourself from the behavior description, don't expect ready-to-paste snippets.
> - Preflight: run `just --list` to confirm recipes are present. Fall back to `uv run …` only if `just` is unavailable.
> - **Commits:** this plan does not prescribe commits. Commit at points that make sense to you (typically after each green task), follow the repo's plain-message convention, and re-run `just check` before any commit.
> - **Local-first invariant:** solving/checking/diagnosing still runs MiniZinc through the managed runtime resolved by `runtime.get_minizinc_binary()` — never a bare `$PATH` lookup. It must not call any LLM, must not introduce LangChain / LangGraph, must not add hidden network calls, must not accept user-provided file paths, and must not execute arbitrary Python or shell.
>
> **Project root:** `/home/bios8086/PycharmProjects/PythonProject/openconstraint-mcp`

---

## Context

The three model-executing MCP tools — `solve_minizinc_model`, `check_minizinc_model`, `find_unsat_core` — already take the model as **inline source text** (`model: str`), not a path: `_run_managed_minizinc` writes it to a private temp `model.mzn` and pins the subprocess `cwd` to that temp dir so cwd-relative `include` statements resolve onto emptiness. There is, however, no way to pass MiniZinc **data** (`.dzn` assignments). A model that declares parameters (`int: n;`) therefore cannot be run through any of these tools unless the values are hard-coded into the model body.

This plan adds an optional inline `data` argument to **all three** tools: the client provides `.dzn` content directly as text, the runtime writes it to a sibling `data.dzn` inside the same isolated temp dir, and passes it to MiniZinc as a positional data file after the model (`model.mzn data.dzn`). This matches the branch name `feature/add-inline-data-support`.

**Why all three.** The server prompt (`server.py:62`) and README (`README.md:180`) prescribe a **validate-before-solve** loop — "never call `solve_minizinc_model` ahead of a clean check" — and the README/prompt point clients at `find_unsat_core` when a solve returns `unsatisfiable`. A parameterized model needs its parameter values at *flatten* time, so without data both `check`'s `-c` compile and `findMUS`'s flatten fail on a parameterized model. If only `solve` accepted data, a client following the prescribed workflow could neither validate nor diagnose the very instance it can now solve. So `data` must flow through all three for the workflow to stay coherent.

## Goal

`solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core` accept an optional `data: str | None` of inline MiniZinc data. When provided, it is written to `data.dzn` in the model's temp dir and passed to the managed `minizinc` binary as a positional data file after the model (`… model.mzn data.dzn`). When omitted (`None`), behavior is byte-identical to today.

Public input contract:

- `model: str` — complete MiniZinc source (already inline; unchanged).
- `data: str | None = None` — optional inline MiniZinc data; `None` means "no data".
- `solver: str = "cp-sat"` (solve/check only), `timeout_ms: int = 30000` — unchanged.

## Non-goals

- Reading model **or** data from a client-supplied filesystem path — both are inline-only, by design.
- Multiple data files / data-file lists, `--cmdline-data`/`-D` key=value injection, or `.json` data.
- Parsing or validating the `.dzn` ourselves — MiniZinc owns data parsing; a malformed `.dzn` surfaces through the normal result (`status="error"`, diagnostic in `stderr`).
- Changing `_parse_unsat_core` / the `UnsatCoreResult` schema, or parsing/restricting the `.dzn` to parameter-only assignments (see Decision 6 — the structured core stays best-effort/model-span-only, raw stdout authoritative).

## Architecture

```
cli  ─►  server  ─►  minizinc  ─►  runtime  ─►  schemas
                       ▲
                       └── solve_model(), check_model(), find_unsat_core() all
                           forward `data` into the shared _run_managed_minizinc()
```

- `_run_managed_minizinc` already owns the temp dir and command construction, so the data file must be written there. It gains a `data: str | None = None` parameter. When `data is not None`, it writes `data.dzn` (UTF-8) into the same `TemporaryDirectory` and appends it to argv **after** the model file (`model.mzn data.dzn`). With no data the model file is the last argument; with data it is the second-to-last and the `.dzn` is last.
- `solve_model`, `check_model`, and `find_unsat_core` each gain `data: str | None = None` and forward it.
- `server.py`'s `solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core` each gain `data: str | None = None`, forwarded to their `minizinc.py` function. The existing `(RuntimeMissingError, MiniZincExecutionError, ValueError) → RuntimeError` mapping is unchanged.
- The `solve_constraint_problem` prompt gets a minimal note: when the model relies on inline data, pass the same `data` to both the check and the solve call. (The prompt does not mention `find_unsat_core`, so it is not referenced there.)
- `schemas.py` is **not** touched — `SolveResult`/`CheckResult`/`UnsatCoreResult` are unchanged; `data` is input-only.

## Key behavior decisions

These pin choices the executor must not silently re-derive:

1. **Pass data as a positional file after the model (`model.mzn data.dzn`); the model file is no longer always last.** Official MiniZinc docs show the canonical order as model-before-data (`minizinc --solver gecode model.mzn data.dzn`). The `-d/--data <file>` flag works for `cp-sat` and the `-c` compile, but the findMUS meta-solver (`org.minizinc.findmus`) re-parses argv with a restricted CLI that **rejects `--data`/`-d`** ("Unrecognized option") and accepts only a *positional* data file — confirmed against the bundled binary during Task 5. The positional form is honored by all three paths, so it is the single shape used. Consequence: when `data` is present `cmd[-1]` is the `.dzn` and `cmd[-2]` is the `.mzn`; the command-shape tests locate the model/data files by suffix and assert `"--data" not in cmd`. With no data, the model file remains `cmd[-1]`, byte-identical to today. (An earlier draft of this plan proposed the `--data` flag to keep the model file last; that was abandoned when Task 5 showed findMUS rejects the flag.)
2. **`data is not None` is the gate.** `None` = omitted (no data file written, no extra argument, command byte-identical to today). A provided string — including `""` — is written verbatim to `data.dzn` and passed positionally; an empty data file is harmless to MiniZinc. We do **not** strip-validate or reject empty data (unlike `model`, which must be non-empty), because empty/whitespace data is a meaningful "no parameters" input, not an error.
3. **Data shares the model's isolation.** `data.dzn` lands in the same private `TemporaryDirectory`, deleted on return, with `cwd` pinned there. No client path is ever opened; the no-path-reading and include-isolation invariants extend to data unchanged.
4. **Data errors are a return, not a raise.** A `.dzn` that fails to parse, or assigns an undeclared/duplicate parameter, is a MiniZinc model error → normal `SolveResult`/`CheckResult`/`UnsatCoreResult` with an error/`status` field, exactly like a model syntax error today. Only environment/argument problems (runtime missing, empty `model`, non-positive `timeout_ms`, OS exec failure) raise.
5. **Surgical scope.** All three functions forward `data`; nothing else changes. No schema changes, no `pyproject.toml` changes, no new dependencies.
6. **`find_unsat_core` structured core stays model-span-only — best-effort, raw stdout authoritative.** `_parse_unsat_core` resolves only `model.mzn` spans (via `_SPAN_PATTERN`, slicing `source` from the model text) and does not add data-file spans. This is **not guaranteed complete**, and the plan must not claim it is. A `.dzn` cannot contain `constraint` *items*, but the MiniZinc spec states that assigning a *decision variable* is equivalent to a constraint — "A value assigned to an unfixed, constrained ... variable makes the assignment act like a constraint" (e.g. `var 1..5: x = 3;` ≡ `var int: x = 3;` + `constraint x in 1..5;`). This tool accepts **arbitrary** `.dzn` and does not parse/restrict it to parameter-only assignments, so a MUS member *can* originate in `data.dzn` when the client assigns a `var`; that span appears in raw `stdout` but **not** in the structured `core`. We keep `_parse_unsat_core`/`UnsatCoreResult` unchanged and rely on the tool's **pre-existing contract** — `core` is *best-effort*, `stdout` is *authoritative* — rather than (a) reimplementing a `.dzn` parser to reject decision-variable assignments (an explicit non-goal, and it would reject legitimate partial-assignment use) or (b) adding a `file`/`data_core` field (schema change, out of scope). README/tool wording must reflect best-effort, never completeness.

## File structure

| File | Action | Responsibility |
| ---- | ------ | -------------- |
| `src/openconstraint_mcp/minizinc.py` | Modify | Add `_DATA_FILENAME = "data.dzn"`; add `data: str | None = None` to `_run_managed_minizinc` (write `data.dzn` + append it positionally after the model when not `None`); add `data: str | None = None` to `solve_model`, `check_model`, and `find_unsat_core` and forward it. `_parse_unsat_core` unchanged. |
| `src/openconstraint_mcp/server.py` | Modify | Add `data: str | None = None` to `solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core`, forward to the respective functions; update the three tool descriptions to mention optional inline data; add a one-line note to the `solve_constraint_problem` prompt to pass the same `data` to both check and solve. |
| `tests/test_minizinc.py` | Modify | Add `solve_model`, `check_model`, and `find_unsat_core` data tests: data provided → a positional `.dzn` is last (`cmd[-1]`), the model `.mzn` is `cmd[-2]`, `data.dzn` contents match, `--data` absent; data omitted → no `.dzn`, model is `cmd[-1]`. The test recorder locates the model/data files by suffix. For `find_unsat_core`, also assert the structured core still resolves from the model text. |
| `tests/test_server.py` | Modify | Add `"data"` to the listed-properties assertions for all three tools; add tool-level tests that inline `data` is threaded to the runtime for each. |
| `README.md` | Modify | Add the `data: str | None = None` bullet to all three tool argument lists; note in the recommended-loop text that the same `data` flows to check and solve (and to `find_unsat_core` after an unsat solve); note in `find_unsat_core` that the structured core is **best-effort/model-span-only** (a data-assigned decision variable is a constraint that stays in `stdout`, not `core`). |

`schemas.py`, `runtime.py`, `runtime_install.py`, `cli.py`, `conftest.py`, and `pyproject.toml` are not modified.

## Module-level interface (signatures only)

```python
# minizinc.py
_DATA_FILENAME: str = "data.dzn"

def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data: str | None = None,   # written to data.dzn, appended positionally after the model
) -> _RunOutcome: ...

def solve_model(
    model: str, *, solver: str = DEFAULT_SOLVER, data: str | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult: ...

def check_model(
    model: str, *, solver: str = DEFAULT_SOLVER, data: str | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult: ...

def find_unsat_core(
    model: str, *, data: str | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
) -> UnsatCoreResult: ...
```

```python
# server.py
def solve_minizinc_model(
    model: str, data: str | None = None, solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult: ...

def check_minizinc_model(
    model: str, data: str | None = None, solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult: ...

def find_unsat_core(
    model: str, data: str | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
) -> UnsatCoreResult: ...
```

Command shape becomes
`[str(binary), "--solver", solver, "--time-limit", str(timeout_ms), *extra_args, <model.mzn>, *((<data.dzn>,) if data is not None else ())]`
— the model file is last when there is no data, and second-to-last (the `.dzn` last) when data is present. (`find_unsat_core` uses `solver=FINDMUS_SOLVER`, `extra_args=()`, exactly as today.)

---

## Task list

Each task is TDD: write tests describing the behavior, run them red, implement, run them green, then verify with the listed command.

### Task 1: `_run_managed_minizinc` + `solve_model` + `check_model` data forwarding

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior:

- [ ] Add `_DATA_FILENAME = "data.dzn"`.
- [ ] Add `data: str | None = None` to `_run_managed_minizinc`. Inside the `TemporaryDirectory` block, when `data is not None`, write `data` to `tmp_dir / _DATA_FILENAME` (UTF-8) and append `str(data_file)` to the command **after** `str(model_file)` (positional `model.mzn data.dzn`). When `data is None`, build the command exactly as today.
- [ ] Add `data: str | None = None` to `solve_model` and `check_model` and forward it to the helper.

Tests (monkeypatch `openconstraint_mcp.minizinc.subprocess.run`; read files at call time, since the temp dir is deleted on return; locate the model/data files by `.mzn`/`.dzn` suffix):

- [ ] **solve: data positional, model second-to-last:** `solve_model(model, data="n = 3;")` → `--data` absent, `cmd[-1]` ends `.dzn` and its contents equal `"n = 3;"`, `cmd[-2]` is the `.mzn`. Mocked `rc=0`, `stdout="==========\n"` → `status="optimal"`.
- [ ] **solve: no data → no `.dzn`; model is `cmd[-1]`.**
- [ ] **check: data positional, model second-to-last** (`-c` still present); mocked `rc=0` → `status="ok"`.
- [ ] **check: no data → no `.dzn`.**
- [ ] **solve: malformed data stays a structured error (not a raise):** mocked `subprocess.run` returns `rc=1`, `stderr="… in file '…/data.dzn' … syntax error …"` (a data-parse/assignment diagnostic). `solve_model(model, data="n = ;")` returns `SolveResult(status="error", stderr=<contains the diagnostic>)` — pins Decision 4 / the parse-error acceptance criterion. (Models on the existing `test_solve_model_returns_structured_result_for_minizinc_compile_error`.)
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green (all pre-existing tests guard the `data=None` path is unchanged).

### Task 2: `find_unsat_core` data forwarding

**Files:** modify `src/openconstraint_mcp/minizinc.py`, modify `tests/test_minizinc.py`.

Behavior:

- [ ] Add `data: str | None = None` to `find_unsat_core` and forward it to `_run_managed_minizinc` (keep `solver=FINDMUS_SOLVER`, `extra_args=()`). Do **not** modify `_parse_unsat_core`.

Tests:

- [ ] **data positional, model second-to-last:** with the existing `_UNSAT_CORE_MODEL` (extend it or use a parameterized variant) and `data=…`, recorded argv has a positional `.dzn` as `cmd[-1]` whose contents match; `cmd[-2]` is the `.mzn`; `--data` absent; `solver` is `FINDMUS_SOLVER`; `-c` absent. With the mocked `_UNSAT_CORE_STDOUT`, `status="mus_found"` and the structured `core` still resolves from the model text (model-only spans, per Decision 6).
- [ ] **no data → no `.dzn`.**
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green.

### Task 3: Expose `data` on all three MCP tools + prompt note

**Files:** modify `src/openconstraint_mcp/server.py`, modify `tests/test_server.py`.

Behavior:

- [ ] Add `data: str | None = None` (after `model`) to `solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core`, forwarding `data=data` to `solve_model` / `check_model` / `_find_unsat_core` respectively. Keep the existing exception→`RuntimeError` mappings.
- [ ] Update the three tool `description`s to note the optional inline `data` argument supplies MiniZinc data (`.dzn` contents) as text, omitted for models that need no external data.
- [ ] Add a single concise sentence to `_SOLVE_CONSTRAINT_PROBLEM_PROMPT`: when the drafted model relies on inline data, pass the **same** `data` to both the check and the solve call. Do **not** rewrite the prompt or alter the existing recommended-loop line (the ordering test anchors on it), and do **not** put both tool identifiers on the new line (the ordering test asserts exactly one line names both tools).

Tests (reuse `_structured` / `_FakeCompletedProcess`):

- [ ] **`data` in input schema (all three):** extend `test_solve_minizinc_model_tool_is_listed`, `test_check_minizinc_model_tool_is_listed`, and `test_find_unsat_core_tool_is_listed` so their asserted property sets include `"data"`.
- [ ] **Inline data threaded to runtime (solve):** capture argv + read `.dzn` at call time; `call_tool("solve_minizinc_model", {"model": …, "data": "n = 3;"})` → captured data == `"n = 3;"`; structured `status == "optimal"`.
- [ ] **Inline data threaded to runtime (check):** parallel → `status == "ok"`.
- [ ] **Inline data threaded to runtime (find_unsat_core):** parallel → `status == "mus_found"` (mock `_UNSAT_CORE_STDOUT`).
- [ ] **Prompt mentions passing data to both:** assert the rendered text references `data` and still names both tools; confirm `..._orders_check_before_solve` and `..._preserves_local_first_boundary` still pass unchanged.
- [ ] Verify: `just pytest tests/test_server.py -v` → green.

### Task 4: README

**Files:** modify `README.md`.

- [ ] Add a `data: str | None = None` bullet to the argument lists of `solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core`: optional inline MiniZinc data (`.dzn` contents — any data assignments, not parameter-only) provided directly as text; omit (or pass `null`) for models that need no external data; written to a private temp file alongside the model and passed to the managed runtime as a positional data file (`model.mzn data.dzn`) — never a client-supplied path.
- [ ] In the recommended-loop text (draft → check → repair → solve → explain), note that when the model uses inline data the **same** `data` is passed to both the check and the solve call.
- [ ] In the `find_unsat_core` entry, keep the existing **best-effort `core` / authoritative `stdout`** framing and add: the structured `core` resolves **model-file** spans only. A `.dzn` cannot contain `constraint` items, but assigning a *decision variable* in data is equivalent to a constraint, so if the client does that, a MUS member can originate in the data file — it appears in raw `stdout` but is **not** added to `core`. Do **not** claim the core is complete.
- [ ] In the solve→diagnose guidance (the existing text that says to call `find_unsat_core` when `solve_minizinc_model` returns `unsatisfiable`), add that the client must pass the **same** `data` to `find_unsat_core` that it passed to the solve call: a parameterized model needs it to flatten at all, and diagnosing a different instance than the one that proved unsat is meaningless.
- [ ] Verify: read the rendered sections; no code to test.

### Task 5: Real-runtime integration smoke for inline data

**Files:** modify `tests/test_minizinc_integration.py`.

The mocked unit tests prove argv shape but **cannot** prove the bundled runtime honors a positional data file — the plan's top risk (and the one that falsified the original `--data` design; see Decision 1). Add real-binary smokes (marked `integration` via the file's existing `pytestmark`, gated by the existing `_require_runtime` autouse skip), one per solver path that takes data. Use a parameterized model so missing/ignored data would *fail* rather than pass — making the test a genuine proof the data was read.

- [ ] **solve honors inline data (cp-sat):** `solve_model("int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;\noutput [\"x=\\(x)\\n\"];", data="n = 4;")` → `status in {"satisfied", "optimal"}` and `"x=4" in result.stdout` (the data forces `x` to `4`).
- [ ] **check honors inline data (-c):** `check_model(<same parameterized model>, data="n = 4;")` → `status == "ok"` (without data this model fails to flatten — the unassigned `n` bounds a variable domain (`var 1..n: x`), so a clean `ok` proves the data was read).
- [ ] **find_unsat_core honors inline data (findMUS):** with `model="int: lo;\nint: hi;\nvar 0..10: x;\nvar 0..10: y;\nconstraint x + y > lo;\nconstraint x + y < hi;\nconstraint x != y;\nsolve satisfy;"` and `data="lo = 5;\nhi = 3;"` → `status == "mus_found"`, and the normalized core sources contain `x + y > lo` and `x + y < hi` (proves the data was read *and* that the model-only core resolves correctly with data present). Phrase the conflict over a *sum* rather than direct single-variable bounds: a `x >= lo` / `x <= hi` contradiction is detected at flatten time and folded into findMUS's hard "background" ("Background is not satisfiable, exiting"), leaving no soft constraints to isolate as a MUS.
- [ ] Verify: `just integration` → green on a machine where `install-runtime` has placed a runtime. (These are excluded from `just check` by the `integration` marker.)

### Task 6: Final `just check`

- [ ] `just check` — lint + typecheck + tests green.

## Acceptance criteria

- `solve_minizinc_model`, `check_minizinc_model`, and `find_unsat_core` each appear in `mcp.list_tools()` with a `data` input property and still return their respective result shapes.
- With `data` provided, argv ends with a positional path ending `.dzn` (contents == the provided text) as `cmd[-1]`, the model `.mzn` is `cmd[-2]`, and `--data` is absent; tests pin this for all three tools.
- With `data` omitted, the command is byte-identical to today (no `.dzn` written; the model `.mzn` is `cmd[-1]`); the full pre-existing suites pass unchanged.
- `data` is written into the model's private temp dir (deleted on return) with `cwd` pinned there; no client filesystem path is read for either model or data.
- Data parse/assignment errors come back as a `status="error"`-style result with the diagnostic in `stderr`, not as an MCP error; a mocked `rc=1`+data-stderr test pins this.
- `find_unsat_core` accepts `data`, can diagnose parameterized unsat models, and its structured `core` still resolves **model-file** constraints from the model text — **best-effort** (a data-assigned decision variable is a constraint that stays in `stdout`, not `core`); `_parse_unsat_core` and `UnsatCoreResult` are unchanged.
- The `solve_constraint_problem` prompt tells clients to pass the same `data` to both check and solve; existing ordering and local-first prompt tests still pass.
- README documents the new `data` argument on all three tools, the data-to-both-calls loop note, the find_unsat_core model-only-core note, and the "pass the same `data` to `find_unsat_core` after an unsat solve" guidance.
- Real-runtime integration smokes (`tests/test_minizinc_integration.py`, `integration`-marked) prove the bundled binary honors the positional data file for the cp-sat, `-c`, and `org.minizinc.findmus` paths via parameterized models; `just integration` is green where a runtime is installed.
- `just check` is green. No telemetry, no new network calls, no LangChain/LangGraph, no bare-`$PATH` MiniZinc, no server-side LLM, no client-provided file paths.

## Known risks

- **Command-shape correctness.** The plan relies on the positional `model.mzn data.dzn` order being honored by the bundled runtime for the cp-sat, `-c`, and findMUS paths. The `-d/--data <file>` flag was tried first but **findMUS rejects it** ("Unrecognized option"; see Decision 1); the positional form works on all three paths and is confirmed by Task 5's `integration` smokes (parameterized models where ignored data would fail, not pass), run via `just integration`. The mocked unit tests only pin argv shape, not runtime acceptance.
- **Empty-string data.** Passing `data=""` writes an empty `data.dzn` and still passes it positionally. Harmless (no parameters assigned) and intentional per Decision 2; documented as "`None` means omitted".
- **`find_unsat_core` core completeness.** The structured core lists model-file spans only and is **best-effort, not guaranteed complete**: a `.dzn` assignment to a decision variable is semantically a constraint (MiniZinc spec) and could be a MUS member that appears in raw `stdout` but not in `core`. We accept and document this (Decision 6) rather than parsing/restricting `.dzn`; it matches the tool's existing best-effort-`core` / authoritative-`stdout` contract. Task 5's integration smoke uses *parameter* data (the deterministic, model-span case); the decision-variable case is covered by documentation, not a brittle span-assertion test.
- **Scope: three tools, not one.** `check` and `find_unsat_core` are included alongside `solve` so the prescribed validate-before-solve-then-diagnose workflow stays coherent for parameterized models. Deliberate, reviewer-driven, justified by the prompt/README workflow — not gold-plating.