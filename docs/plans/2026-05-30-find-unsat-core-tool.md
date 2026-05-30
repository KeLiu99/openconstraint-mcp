# find_unsat_core MCP Tool — Implementation Plan

> **For agentic workers:** Implement task-by-task; steps use checkbox (`- [ ]`) syntax for tracking. Follow AGENTS.md and the `just` recipes for automation. (Optional: the superpowers `subagent-driven-development` or `executing-plans` skills can drive this workflow, but they are not a repo requirement.)
>
> **Plan style:** This document follows AGENTS.md §5 — it is a behavior-first execution guide. Snippets are limited to schemas, signatures, the findMUS parsing invariant, and commands. It deliberately does **not** dump full function or test bodies; implement them from the behavior descriptions.
>
> **Execution gate:** Do not start coding until the user gives an explicit go-signal.

**Goal:** Add `find_unsat_core`, a first-class MCP tool that wraps MiniZinc's `org.minizinc.findmus` to report a minimal unsatisfiable subset (MUS) of an unsatisfiable model, with a structured-but-best-effort result and verbatim raw output preserved.

**Architecture:** Reuse the existing `minizinc.py` layering. A new public `find_unsat_core` calls the shared private runner `_run_managed_minizinc` with `solver="org.minizinc.findmus"`, then maps the raw outcome to a Pydantic `UnsatCoreResult` via a best-effort parser. The server exposes it as an MCP tool with the same environment-error → MCP-error / MiniZinc-outcome → structured-result contract every other tool uses.

**Tech Stack:** Python 3.12 target, Pydantic v2, FastMCP, pytest (+ pytest-asyncio), `uv`/`just` automation, managed MiniZinc runtime.

---

## Non-goals

- No globally-smallest / cardinality-minimum core. findMUS returns *a* minimal subset; the tool must never claim "smallest". Terminology is "minimal unsatisfiable subset" / "MUS".
- No multi-MUS enumeration (`-a`/`-n`), no `--depth`/`--subsolver` tuning, no `--output-mode json` in v0. Default findMUS text output only.
- No new CLI command. MCP tool + library function only.
- No `solver` argument exposed — the solver is hardwired to findMUS.

## Assumptions & Decisions

- **D1 — Layer reuse.** `find_unsat_core` reuses `_run_managed_minizinc(model, solver=FINDMUS_SOLVER, timeout_ms=..., extra_args=())`. This inherits managed-runtime resolution, temp-dir isolation (`cwd`), the `+5s` subprocess grace, UTF-8 handling, empty-model/`timeout_ms<=0` `ValueError`s, and `RuntimeMissingError`/`MiniZincExecutionError`. No bespoke runner.
- **D2 — Status taxonomy** (precedence top→down): `timeout` (subprocess wall-clock cap fired) → `mus_found` (a `MUS:` line is present in stdout) → `error` (nonzero return code, no MUS — e.g. compile/type error, or findMUS solver unavailable; diagnostics in stderr) → `no_core` (ran cleanly, returncode 0, no MUS).
- **D3 — `no_core` is conservative.** It means "findMUS completed and did not report a MUS", **not** "the model is satisfiable". Output/parser limitations can also produce it. In particular, a tight `timeout_ms` can surface as `no_core` rather than `timeout`: if findMUS honors `--time-limit` it stops with rc 0 without tripping the `+5s` hard subprocess cap, so an early stop on a genuinely-UNSAT model reads as `no_core`. `message`, the schema docstring, and the README must all reflect that `no_core` ≠ "no MUS exists".
- **D4 — Structured core, best-effort.** `core: list[UnsatCoreConstraint]` carries optional span coordinates + resolved source text. Parsing is best-effort; `stdout` is authoritative. `core` may be empty even when `status == "mus_found"` (traces unparseable or pointing only at library files). Conversely, `core` is **non-empty only** when `status == "mus_found"`: `_parse_unsat_core` returns no items unless a MUS was reported, and `find_unsat_core` forces `core=[]` for `no_core`, `error`, and `timeout`. A `no_core`/`error` result with a non-empty `core` would be a bug.
- **D5 — `message` is run-specific.** A short per-run summary. Unlike `SolveResult`/`CheckResult` (which have no such field), this diagnostic result adds `message` deliberately; keep it short and run-specific. The "minimal ≠ globally smallest" caveat lives in the README and the `UnsatCoreResult` docstring / `core` field description — **not** repeated in `message` every call.
- **D6 — Trace matching by model filename.** The parser only resolves trace spans whose file basename equals the model file the runner writes. Introduce a shared module constant `_MODEL_FILENAME = "model.mzn"`, use it in `_run_managed_minizinc` (replacing the literal) and in the parser, so stdlib `.mzn` spans are excluded and the two sites cannot drift.
- **D7 — Assumed Traces format (RISK, see Risks).** findMUS prints MiniZinc constraint paths as pipe-delimited `file|startline|startcol|endline|endcol|...` segments separated by `;`, under a `Traces:` section. The parser and the unit-test fixture commit to this format; the integration task confirms it against the real binary and the regex/fixture are tuned together if it differs.
- **D8 — Default timeout.** `DEFAULT_UNSAT_CORE_TIMEOUT_MS = DEFAULT_SOLVE_TIMEOUT_MS` (30_000), named separately with a short comment, mirroring `DEFAULT_CHECK_TIMEOUT_MS`.
- **D9 — Naming.** Library function `find_unsat_core` in `minizinc.py`; MCP tool also named `find_unsat_core`. The tool name is a **user requirement** (deliberately chosen over a `find_minizinc_unsat_core` form that would match the `*_minizinc_model` convention). The server imports the library function aliased (`find_unsat_core as _find_unsat_core`) to dodge the identifier clash with the decorated tool function — the alias is the cost of honoring the required name.
- **D10 — No derived boolean.** The result carries **no** `core_found` field. Clients branch on `status` (`status == "mus_found"`), consistent with `SolveResult`/`CheckResult`, which expose no derived bool. This also avoids a misleading `core_found=True` with `core=[]` when a MUS is reported but its traces don't resolve to model spans.

## Invariants honored

Managed runtime only (via `_run_managed_minizinc`), no network, no telemetry, no global mutable state, no server-side LLM, no PATH solver lookup.

## File Structure

- `src/openconstraint_mcp/schemas.py` — add `UnsatCoreConstraint`, `UnsatCoreStatus`, `UnsatCoreResult`.
- `src/openconstraint_mcp/minizinc.py` — add `FINDMUS_SOLVER`, `DEFAULT_UNSAT_CORE_TIMEOUT_MS`, `_MODEL_FILENAME` (refactor runner to use it), `_parse_unsat_core`, `_slice_source`, `find_unsat_core`.
- `src/openconstraint_mcp/server.py` — import additions + `find_unsat_core` MCP tool.
- `tests/test_minizinc.py` — schema round-trip/validation, parser, and `find_unsat_core` unit tests (mocked subprocess).
- `tests/test_server.py` — tool-listed + happy-path + failure-mode MCP tests.
- `tests/test_minizinc_integration.py` — `@pytest.mark.integration` real-runtime MUS test.
- `README.md` — `## MCP tools` intro count + `find_unsat_core` bullet.

## Reference: example model & representative findMUS output

The contradictory model under test — the fenced block below is the **literal bytes written to `model.mzn`** (no line-number prefixes; the file starts at the `var` line):

```minizinc
var 0..10: x;
var 0..10: y;

constraint x + y > 5;
constraint x + y < 3;
constraint x != y;

solve satisfy;
```

**Line map (display-only, 1-indexed)** used by the fixture spans and `_slice_source`: 1 `var 0..10: x;` · 2 `var 0..10: y;` · 3 *(blank)* · 4 `constraint x + y > 5;` · 5 `constraint x + y < 3;` · 6 `constraint x != y;` · 7 *(blank)* · 8 `solve satisfy;`. **Column note:** in `constraint x + y > 5;` the `c` of `constraint` is column 1, so the expression `x + y > 5` spans columns 12–20 inclusive — span `model.mzn|4|12|4|20` slices to exactly `x + y > 5`.

The MUS is `{x + y > 5, x + y < 3}` (lines 4–5); `x != y` (line 6) is a genuine third constraint outside that conflict and must **not** appear in the parsed core. (Use a disequality, **not** a bound like `x >= 0` or `x >= 2`: MiniZinc folds domain-bound constraints into the variable's bounds during flattening, so they never exist as a separate soft constraint to test exclusion against — verified against real `org.minizinc.findmus`, which reports `soft cons: 2` for the `x >= 2` model but `soft cons: 3` for `x != y`, with the MUS still just the two sum constraints.) Representative findMUS stdout used by the unit-test fixture (the trailing `redefinitions.mzn` span exercises stdlib exclusion):

```
FznSubProblem:  hard cons: 0    soft cons: 3   leaves: 3      branches: 4    Built tree in 0.01 seconds.
MUS: 1 2
Brief: int_lin_le, int_lin_le
Traces: model.mzn|4|12|4|20|;model.mzn|5|12|5|20|;redefinitions.mzn|10|1|10|5|
```

For `model.mzn|4|12|4|20`, slicing the model source at line 4, cols 12–20 (1-indexed, end inclusive) yields `x + y > 5`; line 5 yields `x + y < 3`.

---

## Task 1: Schemas

**Files:**
- Modify: `src/openconstraint_mcp/schemas.py`
- Test: `tests/test_minizinc.py`

**Interface to add:**

```python
class UnsatCoreConstraint(BaseModel):
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    source: str


UnsatCoreStatus = Literal["mus_found", "no_core", "error", "timeout"]


class UnsatCoreResult(BaseModel):
    """Outcome of a findMUS (org.minizinc.findmus) run.

    `core` reports *a* minimal unsatisfiable subset (MUS): constraints that
    are jointly unsatisfiable and from which none can be removed without
    losing unsatisfiability. "Minimal" does NOT mean globally smallest — a
    model may have several MUSes of differing sizes and findMUS returns one.
    `stdout` holds findMUS's raw output and is authoritative; `core` is a
    best-effort structured view and may be empty even when status is
    "mus_found". `no_core` means findMUS finished without reporting a MUS,
    NOT that the model is satisfiable — a tight `timeout_ms` can also surface
    as `no_core` (findMUS may stop at its own --time-limit with rc 0).
    Clients branch on `status`; there is no derived `core_found` flag.
    """

    status: UnsatCoreStatus
    core: list[UnsatCoreConstraint] = Field(
        default_factory=list,
        description=(
            "Best-effort structured view of the minimal unsatisfiable subset "
            "(MUS) — minimal, not necessarily globally smallest. May be empty "
            'even when status is "mus_found"; stdout is the authoritative output.'
        ),
    )
    message: str
    stdout: str
    stderr: str
    elapsed_ms: int
```

- [ ] **Step 1 — Tests first.** In `tests/test_minizinc.py`, add (behavior only):
  - a round-trip test: build a `UnsatCoreResult` with `status="mus_found"`, one `UnsatCoreConstraint(line=4, column=12, end_line=4, end_column=20, source="x + y > 5")`, a `message`, non-empty `stdout`; assert `model_dump()` equals the expected nested dict (including the constraint dict with all five fields).
  - a validation test: constructing `UnsatCoreResult(status="bogus", ...)` raises `pydantic.ValidationError` (mirrors `test_solve_result_rejects_unknown_status`).
- [ ] **Step 2 — Run, expect FAIL.** `just pytest tests/test_minizinc.py -k unsat_core -v` → fails (names undefined).
- [ ] **Step 3 — Implement** the three definitions above in `schemas.py` (place after `CheckResult`, before `InstallConfig`). `Field` is already imported.
- [ ] **Step 4 — Run, expect PASS.** `just pytest tests/test_minizinc.py -k unsat_core -v`.
- [ ] **Step 5 — Verify** `just typecheck` is clean.
## Task 2: minizinc.py — constants, parser, `find_unsat_core`

**Files:**
- Modify: `src/openconstraint_mcp/minizinc.py`
- Test: `tests/test_minizinc.py`

**Signatures / constants to add:**

```python
FINDMUS_SOLVER: str = "org.minizinc.findmus"
DEFAULT_UNSAT_CORE_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS  # findMUS budget; see DEFAULT_CHECK_TIMEOUT_MS note
_MODEL_FILENAME: str = "model.mzn"

def _slice_source(model: str, sl: int, sc: int, el: int, ec: int) -> str: ...
def _parse_unsat_core(stdout: str, model: str) -> tuple[bool, list[UnsatCoreConstraint]]: ...
def find_unsat_core(model: str, *, timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS) -> UnsatCoreResult: ...
```

**Refactor (D6):** in `_run_managed_minizinc`, replace the literal `"model.mzn"` with `_MODEL_FILENAME`. No behavior change; covered by the existing `*_command_shape` tests.

**`_slice_source` behavior:** 1-indexed line/col, end inclusive. Single-line span → `lines[sl-1][sc-1:ec]`. Multi-line → first line from `sc-1`, whole middle lines, last line up to `ec`, joined by `\n`. Out-of-range/invalid indices → fall back to the full referenced line(s); if even that is impossible, return `""`. Never raises.

**`_parse_unsat_core` behavior:**
- `mus_present = any(line.lstrip().startswith("MUS:") for line in stdout.splitlines())`.
- **If not `mus_present`, return `(False, [])` immediately** — do not parse traces. `core` is the structured view of a *reported* MUS, so it must be empty whenever no MUS was reported.
- Otherwise scan stdout with a tolerant regex for pipe-delimited spans: file token ending in `.mzn`, then four ints — `([^\s|;]+\.mzn)\|(\d+)\|(\d+)\|(\d+)\|(\d+)`.
- Keep only matches whose `Path(file).name == _MODEL_FILENAME` (excludes stdlib `.mzn`).
- For each kept span build `UnsatCoreConstraint(line, column, end_line, end_column, source=_slice_source(...))`. Dedupe on the `(line, column, end_line, end_column)` tuple, preserving first-seen order.
- Return `(True, items)` — `items` may still be empty if a MUS was reported but no span resolved to the model file.

**`find_unsat_core` behavior:**
- `outcome = _run_managed_minizinc(model, solver=FINDMUS_SOLVER, timeout_ms=timeout_ms, extra_args=())`.
- If `outcome.timed_out`: `status="timeout"`, `core=[]`, `message="findMUS timed out before reporting a result."`, pass through `stdout`/`stderr`/`elapsed_ms`.
- Else parse `(mus_present, core) = _parse_unsat_core(outcome.stdout, model)` and classify per D2:
  - `mus_present` → `status="mus_found"`, `message=f"findMUS reported a minimal unsatisfiable subset; {len(core)} constraint location(s) resolved from the submitted model."` (when `len(core)==0`, use a message noting none were resolved and to see stdout).
  - elif `outcome.returncode != 0` → `status="error"`, `core=[]`, `message="findMUS did not complete successfully; see stderr."`.
  - else → `status="no_core"`, `core=[]`, `message="findMUS completed without reporting a minimal unsatisfiable subset."` (conservative — not "satisfiable").

- [ ] **Step 1 — Parser tests first** (`tests/test_minizinc.py`, behavior only), using the model + fixture stdout from the Reference section:
  - MUS present: `_parse_unsat_core(fixture_stdout, model)` → first element (`mus_present`) is `True`; exactly two items; `items[0]` has `line=4, column=12, end_line=4, end_column=20` and `"x + y > 5" in items[0].source`; `items[1]` resolves `"x + y < 3"`; **no** item's `source` contains `"x != y"`; the `redefinitions.mzn` span is excluded (still two items).
  - No MUS: `_parse_unsat_core("=====UNKNOWN=====\n", model)` → `(False, [])`.
  - No MUS but trace-like spans present: `_parse_unsat_core("Traces: model.mzn|4|12|4|20|\n", model)` → `(False, [])` — without a `MUS:` line the parser must not emit core items (locks the D4 invariant).
- [ ] **Step 2 — `find_unsat_core` tests first** (mock `openconstraint_mcp.minizinc.subprocess.run` via the existing `_record_subprocess` helper + `fake_minizinc_binary`):
  - **mus_found:** subprocess returns the fixture stdout, rc 0 → `status=="mus_found"`, two `core` items, `result.stdout == fixture_stdout`, `message` mentions "minimal unsatisfiable subset".
  - **command shape:** the recorded `cmd` contains `--solver` immediately followed by `"org.minizinc.findmus"`, contains `--time-limit`, and does **not** contain `-c`; model file written with the model contents; `cwd == model_path.parent`.
  - **no_core:** rc 0, stdout `"=====UNKNOWN=====\n"` (no `MUS:`) → `status=="no_core"`, `core == []`.
  - **error:** rc 1, stderr `"Error: cannot load solver org.minizinc.findmus\n"`, stdout `""` → `status=="error"`, `core == []`, stderr preserved.
  - **timeout:** `subprocess.run` raises `subprocess.TimeoutExpired` built with all four arguments (`cmd`, `timeout`, `output=b"partial"`, `stderr=b""`) — follow the existing `test_solve_model_timeout_with_bytes_payload_decodes` pattern (`tests/test_minizinc.py:442`) → `status=="timeout"`, `stdout=="partial"`.
  - **empty/whitespace model** (`""`, `"\n  \t"`) → `ValueError` (match "empty"); subprocess must not be called.
  - **non-positive timeout** (`0`, `-1`) → `ValueError` (match "positive"); subprocess must not be called.
  - **runtime missing** (`fake_runtime_dir`, no binary) → `RuntimeMissingError` mentioning "install-runtime".
  - **OSError from exec** → `MiniZincExecutionError` mentioning "install-runtime".
  - **default timeout:** recorded `--time-limit` value == `str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)`.
- [ ] **Step 3 — Run, expect FAIL.** `just pytest tests/test_minizinc.py -k "unsat_core or parse_unsat" -v`.
- [ ] **Step 4 — Implement** the constants, `_MODEL_FILENAME` refactor, `_slice_source`, `_parse_unsat_core`, and `find_unsat_core` in `minizinc.py`. Import `UnsatCoreConstraint`, `UnsatCoreResult` from `.schemas`; add `import re` if not present.
- [ ] **Step 5 — Run, expect PASS.** `just pytest tests/test_minizinc.py -v` (whole file, to confirm the `_MODEL_FILENAME` refactor didn't disturb the existing `*_command_shape` tests).
- [ ] **Step 6 — Verify** `just typecheck` and `just lint` clean.
## Task 3: server.py — MCP tool

**Files:**
- Modify: `src/openconstraint_mcp/server.py`
- Test: `tests/test_server.py`

**Imports:** add `DEFAULT_UNSAT_CORE_TIMEOUT_MS` and `find_unsat_core as _find_unsat_core` to the `.minizinc` import; add `UnsatCoreResult` to the `.schemas` import.

**Tool (place after `check_minizinc_model`, before the prompt):**

```python
@mcp.tool(
    description=(
        "Diagnose why a MiniZinc model is unsatisfiable by computing a "
        "minimal unsatisfiable subset (MUS) of its constraints via the "
        "managed runtime's findMUS tool (org.minizinc.findmus). Use it when "
        "solve_minizinc_model returns status 'unsatisfiable' to localize the "
        "conflict. Returns an UnsatCoreResult whose status is 'mus_found', "
        "'no_core' (findMUS finished without reporting a MUS), 'error' (see "
        "stderr), or 'timeout'. `core` is a best-effort structured list of the "
        "conflicting constraints (source span + text) resolved from the "
        "submitted model; `stdout` preserves findMUS's raw output verbatim. "
        "The reported subset is MINIMAL — no constraint can be dropped while "
        "staying unsatisfiable — but NOT necessarily the globally smallest, "
        "and a model may have several."
    )
)
def find_unsat_core(model: str, timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS) -> UnsatCoreResult:
    ...
```

**Body:** call `_find_unsat_core(model, timeout_ms=timeout_ms)`; mirror `solve_minizinc_model`'s except-mapping — catch `(RuntimeMissingError, MiniZincExecutionError, ValueError)` and `raise RuntimeError(str(exc)) from exc`.

- [ ] **Step 1 — Tests first** (`tests/test_server.py`, async, behavior only; reuse `_FakeCompletedProcess`, `_structured`, `fake_minizinc_binary`, `fake_runtime_dir`):
  - **listed:** `find_unsat_core` is in `list_tools()` names and its `inputSchema.properties` includes `{"model", "timeout_ms"}` (and does **not** include `solver`).
  - **happy path:** `subprocess.run` returns the fixture stdout (rc 0); `call_tool("find_unsat_core", {"model": <example model>})` → structured `status=="mus_found"`, `len(core)==2`.
  - **runtime missing:** `fake_runtime_dir` → `call_tool` raises; message contains "install-runtime" and "MiniZinc".
  - **empty model:** `{"model": ""}` → raises; message contains "empty"; subprocess not called.
  - **non-positive timeout:** `{"model": "constraint false;\nsolve satisfy;", "timeout_ms": 0}` → raises; message contains "positive".
- [ ] **Step 2 — Run, expect FAIL.** `just pytest tests/test_server.py -k find_unsat_core -v`.
- [ ] **Step 3 — Implement** the imports + tool.
- [ ] **Step 4 — Run, expect PASS.** `just pytest tests/test_server.py -k find_unsat_core -v`.
- [ ] **Step 5 — Verify** `just typecheck` clean.
## Task 4: README

**Files:**
- Modify: `README.md` (`## MCP tools` section, ~lines 127–221)

- [ ] **Step 1 — Intro count.** Update the section lead-in ("two introspection tools, a model-check tool, and an execution tool") to also mention the unsat-core diagnostic tool.
- [ ] **Step 2 — Add a `find_unsat_core` bullet** after the `solve_minizinc_model` bullet, matching the existing bullets' depth. Cover:
  - **What:** wraps findMUS (`org.minizinc.findmus`) to localize *why* a model is unsatisfiable; complements the loop — when `solve_minizinc_model` returns `"unsatisfiable"`, call `find_unsat_core`.
  - **Arguments:** `model: str` (complete source, must not be empty); `timeout_ms: int = 30000` (positive; `0` is a validation error, not "no timeout").
  - **Returns `UnsatCoreResult`:** `status` (`"mus_found"`, `"no_core"`, `"error"`, `"timeout"`), `core: list[UnsatCoreConstraint]` (`line`/`column`/`end_line`/`end_column`/`source`, best-effort, may be empty even when a MUS was found), `message`, `stdout` (raw findMUS output — authoritative), `stderr`, `elapsed_ms`. Clients branch on `status` (no derived `core_found` flag).
  - **`no_core` is conservative:** it means findMUS reported no MUS, **not** that the model is satisfiable; a tight `timeout_ms` can also surface as `no_core` rather than `timeout` (findMUS may stop at its own `--time-limit`).
  - **Caveat (prominent):** returns **a** minimal unsatisfiable subset — *minimal* means no constraint can be removed while staying unsatisfiable; it is **not** necessarily the globally smallest, and a model may have several.
  - **Failure-mode contract:** environment/argument problems (no runtime, empty model, non-positive timeout, OS exec failure) → MCP errors; findMUS outcomes → a normal `UnsatCoreResult` whose `status` encodes the result.
- [ ] **Step 3 — Verify** the rendered Markdown reads cleanly.
## Task 5: Integration test (real runtime)

**Files:**
- Modify: `tests/test_minizinc_integration.py`

- [ ] **Step 1 — Add an `@pytest.mark.integration` test** (the module already sets `pytestmark` and an autouse `_require_runtime` skip). Behavior:
  - Run `find_unsat_core(<example contradictory model>)` against the real managed runtime.
  - Assert `result.status == "mus_found"`.
  - Assert the **parsed core** contains the contradiction: some `c.source` contains `"x + y > 5"` and some contains `"x + y < 3"` (normalize internal whitespace before matching to tolerate findMUS span granularity).
  - Assert **no** parsed `c.source` contains `"x != y"`. Do **not** assert against raw `stdout` for this — raw output is implementation-dependent.
  - Broaden the module docstring's first line to note it now also covers `find_unsat_core` (keep it minimal).
- [ ] **Step 2 — Run** on a machine with a runtime: `just pytest -m integration -v`. Expected PASS. If the real `Traces:` format differs from the assumed pipe format (D7/Risks), tune the `_parse_unsat_core` regex **and** the Task 2 fixture together, then re-run Task 2 + this test.
- [ ] **Step 3 — Confirm** `just check` (which excludes integration) is green.
---

## Verification (Definition of Done)

- [ ] `just check` green (lint + typecheck + unit tests; integration excluded).
- [ ] `just pytest -m integration -v` green on a runtime-equipped machine (confirms the real findMUS format and the "minimal" assertion).
- [ ] README `## MCP tools` documents `find_unsat_core`, including the minimal-≠-smallest caveat.
- [ ] No new telemetry, network calls, global mutable state, or non-managed solver lookup.

## Acceptance Criteria

1. `find_unsat_core` is exposed as an MCP tool taking `model` + `timeout_ms` (no `solver`), returning `UnsatCoreResult`.
2. It runs `org.minizinc.findmus` through the managed runtime via `_run_managed_minizinc`.
3. On a contradictory model it returns `status="mus_found"` with the conflicting constraints surfaced (structured `core` when parseable, always in raw `stdout`).
4. It never claims "smallest"; terminology is "minimal unsatisfiable subset" / "MUS".
5. Predictable behavior for unavailable findMUS (`error` + stderr), findMUS error (`error`), no MUS (`no_core`, conservative), and subprocess timeout (`timeout`).
6. Environment/argument failures surface as MCP errors; MiniZinc outcomes as a structured result.

## Risks

- **R1 — findMUS output format (primary).** The `Traces:` tokenization (pipe `file|sl|sc|el|ec` vs. other delimiters) and whether Traces print by default are not verifiable without the real binary in this environment. Mitigation: parser is tolerant, `stdout` is always preserved and authoritative, and Task 5 confirms/tunes the regex + fixture against the real runtime. A format mismatch degrades to `core=[]` with full `stdout` — never a crash or a wrong status.
- **R2 — `--time-limit` with findMUS.** The shared runner always passes `--time-limit`. If findMUS rejects it, that surfaces as `status="error"` with the diagnostic in stderr (predictable). If observed in Task 5, special-case findMUS to drop `--time-limit` (the `+5s` subprocess cap still bounds the run).
- **R3 — MUS index count vs resolved core count.** The `MUS:` line lists FlatZinc constraint indices, which need not equal the number of source-level constraints. `message` and `core` intentionally report *resolved source locations*, not the FlatZinc index count, to stay meaningful to the user.
