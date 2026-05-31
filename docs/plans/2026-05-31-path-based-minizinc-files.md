# Path-based MiniZinc file tools (`check/solve/find_unsat_core_files`) — Implementation Plan

> **Executor notes**
>
> - Drive task-by-task; tick `- [ ]` boxes as you go. Each task lists files, behavior, tests, and a verification command — write the code yourself from the behavior description, don't expect ready-to-paste snippets.
> - Preflight: run `just --list` to confirm recipes are present. Fall back to `uv run …` only if `just` is unavailable.
> - **Commits:** this plan does not prescribe commits. Commit at points that make sense (typically after each green task), follow the repo's plain-message convention (no `Co-Authored-By` footer), and re-run `just check` before any commit.
> - **Local-first invariant:** both modes run MiniZinc through the managed runtime resolved by `runtime.get_minizinc_binary()` — never a bare `$PATH` lookup. No LLM calls, no MCP sampling, no LangChain/LangGraph, no hidden network calls, no upload. The new tools accept **caller-named model/data paths**; MiniZinc's own include reads then follow the documented **mode-specific scope** (default isolation is cwd-relative, not a sandbox) — see "Security & scope posture". That is the intended, scoped expansion.
>
> **Project root:** `/home/bios8086/PycharmProjects/PythonProject/openconstraint-mcp`
> **Branch:** `feature/handle-path-based-MiniZinc-file`

---

## Context

The three model-executing MCP tools — `solve_minizinc_model`, `check_minizinc_model`, `find_unsat_core` — take the model (and now data) as **inline source text**. `_run_managed_minizinc` (`minizinc.py`) writes that text to a private temp `model.mzn` (+ optional `data.dzn`) and pins the subprocess `cwd` to the temp dir, so cwd-relative `include` statements resolve onto emptiness. This is safe and ideal for small/medium models the client LLM drafts, but it forces the agent to read an entire `.mzn`/`.dzn` from disk and thread the whole contents through MCP arguments. For large local models that is awkward and wasteful.

This plan adds three **path-based** sibling tools that accept explicit, user-provided local file paths, read the model/data from disk, and run the managed runtime — behaving close to normal MiniZinc CLI usage. The inline tools are unchanged and remain the default for ephemeral/isolated text workflows.

The central design problem is **includes**. A `.mzn` may contain `include "helpers.mzn";`. If the server runs MiniZinc with `cwd=model_path.parent`, MiniZinc reads files beyond the two the user named — convenient, but it expands the read scope. The product decision is **convenience over strict include isolation for this mode**, surfaced through an explicit, default-off opt-in.

## Goal

Add `check_minizinc_files`, `solve_minizinc_files`, and `find_unsat_core_files` MCP tools that read a model (and optional data) from local paths and run the managed runtime, returning the **same** `CheckResult` / `SolveResult` / `UnsatCoreResult` shapes as the inline tools. An `allow_local_includes: bool = False` flag selects between **isolation-preserving** (default) and **CLI-like** include behavior.

Public input contract (all three):

- `model_path: str` — path to a local `.mzn` file. Required; must exist and be a regular file.
- `data_path: str | None = None` — path to a local `.dzn` file, or `None`.
- `allow_local_includes: bool = False` — see "Include-handling decision".
- `solver: str = "cp-sat"` (solve/check only), `timeout_ms: int = 30000` — same semantics as the inline tools.

## Non-goals

- **Changing the inline tools' behavior or signatures.** They keep their no-path guarantee. We only *refactor out shared helpers* they already need (sanctioned by the brief).
- **A regex/static include classifier or pre-flight "scan and refuse".** Rejected on purpose — see the rationale below. MiniZinc owns include resolution.
- **Schema changes.** No new result fields, no `hint` field, no mode echo. Result shapes are byte-identical to the inline tools.
- **Multiple model/data files, `-I` include dirs, `-D`/`--cmdline-data`, `.json` data, globbing, or model directories.** One model path, one optional data path.
- **A new CLI subcommand.** This is MCP-tool surface only (matching how the inline tools shipped).
- **Sandboxing/jailing `allow_local_includes=True`.** In that mode MiniZinc may read any file reachable via includes from the model's directory; that is the accepted trade-off, documented, not prevented.

## Architecture

```
cli  ─►  server  ─►  minizinc  ─►  runtime  ─►  schemas
                       ▲
        new *_files ───┤  server tools (str paths) → minizinc.*_model_path() (Path)
        tools          │
                       └─ isolated mode: read text → delegate to existing solve_model/
                          check_model/find_unsat_core (same temp-dir isolation as inline)
                          allow mode: run managed binary on the REAL path, cwd=model.parent
```

- The path tools live in `server.py` (string→`Path` at the boundary, map errors to MCP errors) and `minizinc.py` (the actual logic, testable without MCP). Server stays thin.
- **Isolated mode (`allow_local_includes=False`)** reads `model_path` (and `data_path`) as text and delegates to the existing inline `solve_model` / `check_model` / `find_unsat_core`. Those already write the text into a private temp dir and pin cwd there, so isolation is identical to the inline tools — *relative* includes resolve against the empty temp dir and fail; stdlib includes still resolve via the binary's library path. This is **cwd-relative** isolation, not a sandbox: an **absolute-path** include (or `../`-traversal out of the temp dir) is opened by MiniZinc directly, exactly as in the inline tools today (MiniZinc only applies include-search-path handling when the include name is *not* a complete path — see the spec's include items).
- **Allow mode (`allow_local_includes=True`)** runs the managed binary on the **real** `model_path` (and positional `data_path`) with `cwd=model_path.parent`, so relative includes resolve like normal CLI usage. This needs a small new runner that reuses the inline runner's subprocess/outcome core.
- `schemas.py` is **not** touched.

## Include-handling decision and rationale

**Decision: a single `allow_local_includes: bool = False` flag, no static pre-detection. Default isolation is preserved by delegating to the inline path; the opt-in runs from the model's directory. When isolation blocks a local include, MiniZinc's own "cannot open include file …" diagnostic is returned as a normal `status="error"` result, and the tool *description* tells the agent the actionable next step (retry with `allow_local_includes=true` if the model's directory is trusted).**

Why this shape, point by point against the brief's questions:

1. **"Can/should the server detect include references before solving?"** — It *can* (regex `include "…"`), but it *should not* gate on it. MiniZinc resolves an include name against its full search path: the model's directory **and** the solver's standard library. A bare `include "globals.mzn";` is the single most common real include and is **stdlib**, not local. A pre-scan cannot reliably tell stdlib from local without reimplementing MiniZinc's resolver, so it would either false-positive (refuse a legitimate globals-only model in the safe default mode — unacceptable) or false-negative (miss includes and add no value). MiniZinc is the authority; we defer to it.
2. **"Should the server return an actionable error like 'model uses local include files; rerun with allow_includes=true'?"** — Yes, but realized without a heuristic and without a schema change. In isolated mode a model with an unresolved *relative* local include fails exactly as the inline `check`/`solve` already does today: a structured result with `status="error"` and MiniZinc's verbatim "cannot open include file 'helpers.mzn'" in `stderr` (the inline `check_minizinc_model` description already lists *missing-include* as a returned compile error). The **actionable guidance** lives in the tool `description`: it states the two modes and tells the agent that an include-resolution failure under the default likely means relative local includes, to be retried with `allow_local_includes=true` only if the user trusts the model's directory. The "confirmation flow" is thus the MCP-idiomatic one: deterministic server returns a structured, recognizable error → the **client's** LLM surfaces it to the user and decides whether to retry with the flag. (Human-in-the-loop stays on the client, where AGENTS.md requires all LLM interaction to live; the server never prompts, never samples.) Note this guidance covers *relative* includes only; an **absolute-path** include resolves in both modes (it is not "blocked", so no actionable error fires) — a known limitation, not a guard the server provides.
3. **`allow_local_includes=True`** → run the managed binary on the real path with `cwd=model_path.parent`; relative includes work like the CLI.
4. **`allow_local_includes=False`** (default) → the same cwd-relative isolation as the inline tools: we realize "copy into a private temp dir" by reading the file text and delegating to the inline functions, which write it into a private temp dir and pin cwd — behaviorally identical to a copy, with maximal reuse. *Relative* includes that aren't present fail with MiniZinc's clear diagnostic; absolute-path includes are still opened (the cwd-relative-not-sandbox limitation above).

**Net:** simple and usable. One boolean, no fragile classifier, no schema growth, and the safe behavior is the default. If a future need for proactive scanning emerges it can be layered on without changing this contract.

## Security & scope posture

This is the first server surface that reads caller-named local files, so state it plainly (no AGENTS.md invariant forbids local file reads — the invariants forbid network, upload, telemetry, LLM/sampling, and `$PATH` MiniZinc, all still honored):

- The tools read the files the caller names. In **isolated mode** the runtime normally sees only the model + data text (plus the stdlib), but isolation is **cwd-relative, not a sandbox**: a model that uses an **absolute-path** include — `include "/abs/other.mzn";` — or `../`-traversal will still cause MiniZinc to read that file, identically to the inline tools today. In **allow mode** MiniZinc may additionally read any file reachable via *relative* includes from `model_path.parent` — the documented, opt-in trade-off. Either way the threat model is "a local user pointing the tool at their own files", and allow mode is no broader than what that user could read by hand.
- Paths are resolved to absolute (`Path.resolve()`) inside `_validate_model_data_paths`, which **returns** the resolved paths the callers then use for read/argv/cwd — so `cwd`, argv, and error messages are unambiguous and validation and execution always act on the *same* path; relative inputs resolve against the server process's working directory (document: prefer absolute paths). `resolve()` follows symlinks — a symlinked model the caller named is allowed (the caller named it).
- Still **no network, no upload, no telemetry, no LLM/sampling**. Reading a local file the caller pointed at is not exfiltration.

## Key behavior decisions

1. **`minizinc.py` takes `Path`, `server.py` takes `str`.** Per AGENTS.md ("`pathlib.Path` for filesystem work; do not pass raw strings around as paths"), the string→`Path` conversion happens once, at the server boundary.
2. **Path validation resolves, validates, and *returns* the resolved paths, before any subprocess.** `_validate_model_data_paths` resolves each input to absolute (`Path.resolve()`), raises `ValueError` with the offending path if `model_path` is missing/not-a-regular-file (same for `data_path` when provided) **and** if the model file is empty/whitespace-only, then **returns `(model_path, data_path)` resolved**. Callers rebind to those returned paths and use them for *all* of read/argv/cwd — so validation and execution can never act on different paths (the bug a `-> None` signature + a separate `Path(...)` in the runner would create for relative inputs, where `cwd=model_path.parent` plus a relative argv double-counts the subdir). This reuses the server's existing `(RuntimeMissingError, MiniZincExecutionError, ValueError) → RuntimeError` mapping unchanged. We validate **existence + is-file + non-empty model**, not suffix — MiniZinc owns suffix semantics. The **non-empty model check is explicit here** (not delegated) so *both* modes behave identically: isolated mode would otherwise inherit the inline `ValueError("model must not be empty")` while allow mode would let MiniZinc return `status="error"` — validating up front makes both raise `ValueError`. `data_path` emptiness is **not** checked (empty data is a valid "no parameters" input, matching the inline `data` contract). Every read of a path file's text goes through a small `_read_text_utf8` helper that reads UTF-8 and wraps `UnicodeDecodeError` as `ValueError(f"… is not valid UTF-8: {path}")` — so a non-UTF-8 **model** surfaces as a clear MCP error with the offending path in **both** modes (validation reads the model up front in both), and a non-UTF-8 **data** file does the same in isolated mode (where we read it); in allow mode the data file is read by MiniZinc, so a bad encoding there returns a normal `status="error"`.
3. **Isolated mode delegates to the inline functions; allow mode uses a real-path runner.** Delegation reuses the inline temp-dir isolation, the positional-data ordering, and the findMUS `model.mzn` span parsing for free. Allow mode is the only path that needs new run code.
4. **findMUS span parsing is parameterized by the model filename (basename match — a known best-effort limitation).** Inline/isolated keep the `model.mzn` default. In **allow mode** the model file is the user's real basename (e.g. `nurses.mzn`), so `_iter_model_spans`/`_parse_unsat_core` gain a `model_filename` parameter; allow-mode `find_unsat_core_files` passes `model_path.name`. MUS spans from *included* files remain filtered out of the structured `core` (they appear in raw `stdout`) — same **best-effort `core` / authoritative `stdout`** contract the inline tool already documents for data-assigned decision variables. The filter matches on **basename** (`Path(token).name == model_filename`), so an included file that shares the entry model's basename in a *different directory* (entry `/a/model.mzn` that includes `/b/model.mzn`) could have its spans mis-attributed to the entry model. We accept this as a documented limitation of the best-effort core (raw `stdout` stays authoritative) rather than match full paths — findMUS's exact trace-path format for absolute argv is unconfirmed without the binary; if Task 5's integration run shows it emits resolvable full paths, the filter can be tightened to full-path matching without a contract change.
5. **Reuse, don't duplicate, the run/outcome/result logic.** Extract (a) `_invoke_minizinc(cmd, *, timeout_ms, cwd) -> _RunOutcome` — the `subprocess.run` + timeout-grace + `TimeoutExpired`/`OSError` handling + outcome capture, currently inline in `_run_managed_minizinc`; and (b) the small outcome→result builders the inline functions use. Both inline and allow-mode runners then share them. The inline functions' **public behavior and signatures are unchanged** — existing tests pin the inline command shape and `cwd=tmp_dir` and must stay green.
6. **Tool/param naming.** MCP tools: `check_minizinc_files`, `solve_minizinc_files`, `find_unsat_core_files`. minizinc-layer functions: `check_model_path`, `solve_model_path`, `find_unsat_core_path`. Param order groups paths first: `model_path, data_path, allow_local_includes[, solver], timeout_ms`.
7. **The `solve_constraint_problem` prompt is not modified.** The path tools are discoverable via the tool list and their descriptions; touching the prompt risks the existing order/“both-tools-on-one-line” prompt regressions for no clear workflow gain. (Decision recorded so it isn't silently re-derived.)

## File structure

| File | Action | Responsibility |
| ---- | ------ | -------------- |
| `src/openconstraint_mcp/minizinc.py` | Modify | Extract `_invoke_minizinc` + outcome→result builders; parameterize `_parse_unsat_core`/`_iter_model_spans` by `model_filename`; add `_validate_model_data_paths`, `_run_managed_minizinc_paths`, and the three public `*_model_path` functions. |
| `src/openconstraint_mcp/server.py` | Modify | Register `check_minizinc_files`, `solve_minizinc_files`, `find_unsat_core_files`; convert `str`→`Path`; map errors to MCP errors; rich descriptions covering both include modes + the read-scope note + (for findMUS) the model-only-core caveat. |
| `tests/test_minizinc_files.py` | Create | Unit tests for the path functions (both modes, validation, data threading, findMUS filename) — mock `subprocess.run`, use real `tmp_path` files. |
| `tests/test_server.py` | Modify | Tool-listing (incl. `allow_local_includes`, and `solver` absent for findMUS), happy paths, allow-mode threading, path-not-found MCP error, runtime-missing MCP error. |
| `tests/test_minizinc_integration.py` | Modify | Real-runtime two-mode proof: stdlib include works isolated; relative local include fails isolated + succeeds allow; absolute-path include not blocked under default isolation; findMUS allow-mode core. |
| `README.md` | Modify | New "Path-based file tools" subsection: the three tools, the two include modes + `allow_local_includes`, path validation, the security/read-scope posture, and the model-only-core caveat. |

`schemas.py`, `runtime.py`, `runtime_install.py`, `cli.py`, `prompts.py`, `conftest.py`, and `pyproject.toml` are not modified.

## Module-level interface (signatures only)

```python
# minizinc.py — public additions (take Path; keyword-only after model_path)
def solve_model_path(
    model_path: Path, *, solver: str = DEFAULT_SOLVER, data_path: Path | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS, allow_local_includes: bool = False,
) -> SolveResult: ...

def check_model_path(
    model_path: Path, *, solver: str = DEFAULT_SOLVER, data_path: Path | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS, allow_local_includes: bool = False,
) -> CheckResult: ...

def find_unsat_core_path(
    model_path: Path, *, data_path: Path | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS, allow_local_includes: bool = False,
) -> UnsatCoreResult: ...

# minizinc.py — shared internals
def _invoke_minizinc(cmd: Sequence[str], *, timeout_ms: int, cwd: str) -> _RunOutcome: ...
def _run_managed_minizinc_paths(           # allow-mode runner; cwd = str(model_path.parent)
    model_path: Path, *, solver: str, timeout_ms: int,
    extra_args: Sequence[str], data_path: Path | None = None,
) -> _RunOutcome: ...
def _read_text_utf8(path: Path) -> str: ...  # read UTF-8; wrap UnicodeDecodeError as ValueError(<path>)
def _validate_model_data_paths(           # resolves to absolute, validates, returns resolved paths
    model_path: Path, data_path: Path | None,
) -> tuple[Path, Path | None]: ...        # raises ValueError on missing/non-file/empty-model/non-utf8-model
def _parse_unsat_core(
    stdout: str, model: str, *, model_filename: str = _MODEL_FILENAME,
) -> tuple[bool, list[UnsatCoreConstraint]]: ...
```

```python
# server.py — new tools (take str paths)
def check_minizinc_files(
    model_path: str, data_path: str | None = None, allow_local_includes: bool = False,
    solver: str = DEFAULT_SOLVER, timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
) -> CheckResult: ...

def solve_minizinc_files(
    model_path: str, data_path: str | None = None, allow_local_includes: bool = False,
    solver: str = DEFAULT_SOLVER, timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
) -> SolveResult: ...

def find_unsat_core_files(
    model_path: str, data_path: str | None = None, allow_local_includes: bool = False,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
) -> UnsatCoreResult: ...
```

**Allow-mode command shape** (managed binary, `cwd=str(model_path.parent)`):
`[str(binary), "--solver", solver, "--time-limit", str(timeout_ms), *extra_args, str(model_path), *((str(data_path),) if data_path is not None else ())]`
— `extra_args=("-c",)` for check, `()` for solve, `()` + `solver=FINDMUS_SOLVER` for findMUS. Positional `model.mzn data.dzn` order (findMUS rejects `--data`; established by the inline-data plan).

**Isolated-mode behavior:** read the model (and data) text via `_read_text_utf8` and call the existing `solve_model`/`check_model`/`find_unsat_core` with `data=…`. (UTF-8 read/write matches the inline tools' existing UTF-8 assumption; a non-UTF-8 file becomes a clear `ValueError` rather than an opaque decode traceback.)

---

## Task list

Each task is TDD: write tests describing the behavior, run them red, implement, run them green, verify with the listed command. Unit tests mock `openconstraint_mcp.minizinc.subprocess.run` and use real `tmp_path` files for the on-disk model/data; the recorder reads any written `.dzn` at call time (the temp dir is deleted on return) and locates model/data args by `.mzn`/`.dzn` suffix.

### Task 1: Refactor shared internals (no behavior change)

**Files:** modify `src/openconstraint_mcp/minizinc.py` (and run existing suites as the guard).

Behavior:

- [ ] Extract `_invoke_minizinc(cmd, *, timeout_ms, cwd) -> _RunOutcome` from `_run_managed_minizinc`'s body: compute `subprocess_timeout = (timeout_ms / 1000) + 5`, run `subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=subprocess_timeout, cwd=cwd)`, return the same `_RunOutcome` on success, the same timeout `_RunOutcome` on `TimeoutExpired`, and raise `MiniZincExecutionError` (using `cmd[0]` for the binary in the message) on `OSError`. `_run_managed_minizinc` keeps writing temp files, building its cmd, and now calls `_invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(tmp_dir))`.
- [ ] Extract small outcome→result builders used by the inline functions: a solve builder (timeout → timeout result; else `_parse_status`), a check builder (timeout → timeout; else `ok`/`error` by returncode), and an unsat builder (timeout/mus/error/no_core branches). `solve_model`/`check_model`/`find_unsat_core` call them.
- [ ] Parameterize `_iter_model_spans`/`_parse_unsat_core` with `model_filename: str` (default `_MODEL_FILENAME`); the unsat builder accepts and forwards it (default keeps `model.mzn`).

Tests:

- [ ] No new tests here; the **entire existing** `tests/test_minizinc.py` is the regression guard.
- [ ] Verify: `just pytest tests/test_minizinc.py -v` → green, unchanged.

### Task 2: Path validation + isolated-mode path functions

**Files:** modify `src/openconstraint_mcp/minizinc.py`, create `tests/test_minizinc_files.py`.

Behavior:

- [ ] Add `_read_text_utf8(path) -> str`: `path.read_text(encoding="utf-8")`, catching `UnicodeDecodeError` and re-raising `ValueError(f"{path} is not valid UTF-8")`.
- [ ] Add `_validate_model_data_paths(model_path, data_path) -> tuple[Path, Path | None]`: resolve each to absolute (`Path.resolve()`); raise `ValueError(f"model_path does not exist: …")` / `"… is not a file: …"` if `model_path` is missing or not a regular file (same for `data_path` when not `None`); read the resolved model file via `_read_text_utf8` (so a non-UTF-8 model raises the wrapped `ValueError` here, in both modes) and raise `ValueError("model file is empty: …")` if that text is empty/whitespace-only; **return the resolved `(model_path, data_path)`**. Runs before any subprocess.
- [ ] Add `solve_model_path`/`check_model_path`/`find_unsat_core_path` with the signatures above. Each first does `model_path, data_path = _validate_model_data_paths(model_path, data_path)` and uses **only** those resolved paths thereafter. With `allow_local_includes=False`, read `model_path` (and `data_path`) text via `_read_text_utf8` and delegate to the matching inline function (`solve_model`/`check_model`/`find_unsat_core`) passing `data=…`. (Allow mode lands in Task 3.)

Tests (`tests/test_minizinc_files.py`):

- [ ] **isolated solve delegates with isolation:** write a real `model.mzn` under `tmp_path`; `solve_model_path(model_path, allow_local_includes=False)` with mocked `rc=0`, `stdout="==========\n"` → `status="optimal"`; recorded argv's model arg basename is `model.mzn` **and** lives under a *different* temp dir (not `model_path.parent`), and `cwd` is that temp dir — proving delegation/isolation, not the user's directory.
- [ ] **isolated data threaded:** with `data_path` (real `.dzn`), recorded argv has a positional `.dzn` as `cmd[-1]` whose contents equal the file's text; model `.mzn` is `cmd[-2]`.
- [ ] **isolated check / findMUS** parallel happy paths (`-c` present for check; `solver=FINDMUS_SOLVER`, `-c` absent, mocked `_UNSAT_CORE_STDOUT` → `status="mus_found"`, core from the model text).
- [ ] **validation (`_validate_model_data_paths` directly):** missing `model_path` → `ValueError`; `model_path` is a directory → `ValueError`; `data_path` given but missing → `ValueError`; empty / whitespace-only model file → `ValueError`; a valid model (+ optional data) → returns the **resolved absolute** `(model_path, data_path)`. One test each (subprocess is never reached — this is the path layer).
- [ ] **every public function validates (all three, both modes):** parametrize over `solve_model_path` / `check_model_path` / `find_unsat_core_path` × `allow_local_includes ∈ {False, True}`: a missing `model_path` raises `ValueError` and an empty/whitespace-only model file raises `ValueError`, with `subprocess.run` **never called** (a fail-if-invoked monkeypatch). This pins that all three call `_validate_model_data_paths` before any run, in both modes — not just `solve_model_path`.
- [ ] **non-UTF-8 model is a clear `ValueError` (both modes):** write a `.mzn` containing invalid UTF-8 bytes (e.g. `b"\xff\xfe"`); `solve_model_path(bad_model, allow_local_includes=False)` **and** `…=True` both raise `ValueError` whose message contains the path and "UTF-8", with `subprocess.run` **never called** — pins the `_read_text_utf8` wrapping in validation (Decision 2's "clear errors" bar) rather than an opaque `UnicodeDecodeError` traceback.
- [ ] **isolated unresolved local include is a structured error:** mock `subprocess.run` to return `rc=1`, `stderr="…cannot open included file 'helpers.mzn'…"`; `solve_model_path(model_with_local_include, allow_local_includes=False)` → `SolveResult(status="error")` with the diagnostic preserved in `stderr` (pins Decision 2 — failure is returned, not raised, and not pre-detected).
- [ ] Verify: `just pytest tests/test_minizinc_files.py -v` → green.

### Task 3: Allow-mode runner + path functions

**Files:** modify `src/openconstraint_mcp/minizinc.py`, extend `tests/test_minizinc_files.py`.

Behavior:

- [ ] Add `_run_managed_minizinc_paths(model_path, *, solver, timeout_ms, extra_args, data_path=None)`. It receives **already-resolved absolute** paths from the public functions. Guard `timeout_ms > 0` (`ValueError`) and runtime-installed (`RuntimeMissingError`), resolve the managed binary, build the allow-mode command shape above, and call `_invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(model_path.parent))`.
- [ ] In each `*_model_path`, add the `allow_local_includes=True` branch: call `_run_managed_minizinc_paths` (solve: `extra_args=()`; check: `("-c",)`; findMUS: `extra_args=()`, `solver=FINDMUS_SOLVER`) and pass the outcome to the matching result builder. For `find_unsat_core_path` allow mode, read `model_path` text (UTF-8) **only** to slice `source`, and call the unsat builder with `model_filename=model_path.name`.

Tests:

- [ ] **allow solve runs real path from parent:** `solve_model_path(model_path, allow_local_includes=True)` → argv model arg is exactly `str(model_path.resolve())`; `cwd == str(model_path.resolve().parent)`; mocked `rc=0` → `status="optimal"`.
- [ ] **allow mode resolves relative input (no double-counting):** pass a *relative* `model_path` (e.g. `monkeypatch.chdir(tmp_path)`, then `Path("sub/model.mzn")` for a real file at `tmp_path/sub/model.mzn`); argv model arg and `cwd` are the **resolved absolute** path / its parent — *not* `cwd="sub"` with argv `"sub/model.mzn"` (which would make the subprocess look under `sub/sub/`). Pins the resolution-contract fix (Decision 2).
- [ ] **allow data positional:** `data_path` provided → `cmd[-1]` is `str(data_path.resolve())`, `cmd[-2]` is the model path.
- [ ] **allow check:** `-c` present, real path, `cwd=parent`.
- [ ] **allow findMUS filename:** mock findMUS stdout whose `Traces` reference the **real** model basename (e.g. `nurses.mzn|4|12|4|20|`) plus a differently-named included-file span (e.g. `helpers.mzn|…`); `find_unsat_core_path(model_path, allow_local_includes=True)` → `status="mus_found"`, structured `core` resolves the entry-file spans (source sliced from the real model text) and **excludes** the `helpers.mzn` span (model-only filter). Pins Decision 4.
- [ ] **duplicate-basename collision is documented behavior:** mock findMUS stdout with a span whose token is an included file that shares the entry model's basename (e.g. entry `model_path` is `.../a/model.mzn`, trace token `model.mzn` actually from `.../b/model.mzn`); assert the current basename filter **does** attribute it to the core, and add a code comment + this test naming it a known best-effort limitation (raw `stdout` authoritative). This documents Decision 4's limitation rather than silently leaving it untested.
- [ ] **allow runtime-missing / non-positive timeout / OSError** via `_run_managed_minizinc_paths`: `RuntimeMissingError` / `ValueError("positive")` / `MiniZincExecutionError` respectively (mirror the inline guard tests).
- [ ] Verify: `just pytest tests/test_minizinc_files.py -v` → green.

### Task 4: Register the three MCP tools

**Files:** modify `src/openconstraint_mcp/server.py`, extend `tests/test_server.py`.

Behavior:

- [ ] Register `check_minizinc_files`, `solve_minizinc_files`, `find_unsat_core_files` with the signatures above. Each converts `model_path`/`data_path` (`str`) to `Path` (`Path(data_path) if data_path is not None else None`), calls the matching `*_model_path` with `allow_local_includes`/`solver`/`timeout_ms`, and maps `(RuntimeMissingError, MiniZincExecutionError, ValueError) → RuntimeError` (unchanged pattern).
- [ ] Write rich `description`s: each reads the model (and optional data) from the given **local file paths** on the machine running the server; defaults to **isolated** execution (normal sibling relative includes from the original model directory will not resolve and surface as `status="error"`; absolute-path includes and `../`-traversal are not blocked); set `allow_local_includes=true` **only if the model's directory is trusted** to run from `model_path.parent` so relative includes resolve like the CLI; standard-library includes (`globals.mzn`, etc.) work in both modes. For `find_unsat_core_files`, add the **best-effort `core` / authoritative `stdout`**, model-entry-file-only caveat (allow-mode MUS members from included files appear in `stdout`, not `core`).

Tests (`tests/test_server.py`, reuse `_structured`/`_FakeCompletedProcess`/`_record_data_run`):

- [ ] **tools listed with expected input properties:** `solve_minizinc_files`/`check_minizinc_files` expose `{model_path, data_path, allow_local_includes, solver, timeout_ms}`; `find_unsat_core_files` exposes `{model_path, data_path, allow_local_includes, timeout_ms}` and **not** `solver`.
- [ ] **happy path each tool (isolated default):** real `tmp_path` model, mocked subprocess → `optimal` / `ok` / `mus_found`.
- [ ] **allow mode threaded (one tool suffices):** `solve_minizinc_files(model_path, allow_local_includes=True)` → recorded `cwd == str(model_path.resolve().parent)`.
- [ ] **path-not-found surfaces actionable MCP error:** `call_tool("solve_minizinc_files", {"model_path": "<nonexistent>"})` raises; message contains the offending path; subprocess not called.
- [ ] **runtime-missing surfaces actionable error (one tool):** with `fake_runtime_dir`, allow-mode call raises with `install-runtime` + `MiniZinc` in the message.
- [ ] Verify: `just pytest tests/test_server.py -v` → green.

### Task 5: Real-runtime integration (two-mode proof)

**Files:** modify `tests/test_minizinc_integration.py` (`integration`-marked via the file's `pytestmark`, gated by the existing `_require_runtime` autouse skip).

The mocked units pin argv/cwd but cannot prove the bundled runtime actually resolves (or refuses) includes per mode. Build real files under `tmp_path`.

- [ ] **stdlib include works isolated:** a model that does `include "globals.mzn"; … alldifferent([...]) …`; `check_model_path(model_path, allow_local_includes=False)` → `status="ok"`. Proves the safe default does **not** break global-constraint models.
- [ ] **local include fails isolated:** write `entry.mzn` containing `include "helpers.mzn";` plus a sibling `helpers.mzn`; `check_model_path(entry.mzn, allow_local_includes=False)` → `status="error"`, `stderr` references the unresolved include. Proves isolation actually blocks the local include.
- [ ] **same local include succeeds allow:** `solve_model_path(entry.mzn, allow_local_includes=True)` (or `check`) → `status in {"ok","satisfied","optimal"}`. Proves `cwd=parent` resolves the sibling — the core of the two-mode design.
- [ ] **absolute-path include is NOT blocked by default isolation (documented limitation):** write `helper.mzn` somewhere under `tmp_path` and an `entry.mzn` whose include uses its **absolute** path (`include "<abs>/helper.mzn";`); `check_model_path(entry.mzn, allow_local_includes=False)` → `status="ok"` (the absolute include resolves *despite* isolation). Encodes the truthful "cwd-relative, not a sandbox" claim as a test, not just prose — if this ever starts failing because a real guard was added, the contract changed deliberately.
- [ ] **findMUS allow-mode core:** an unsat model on disk whose conflicting constraints live in the entry file; `find_unsat_core_path(model_path, allow_local_includes=True)` → `status="mus_found"` and normalized core sources contain the entry-file constraints (proves the real-basename span filter).
- [ ] Verify: `just integration` → green where a runtime is installed (excluded from `just check`).

### Task 6: README

**Files:** modify `README.md`.

- [ ] Add a "Path-based file tools" subsection under **MCP tools**: document `check_minizinc_files`, `solve_minizinc_files`, `find_unsat_core_files` — arguments (`model_path`, `data_path`, `allow_local_includes`, `solver` [solve/check], `timeout_ms`), the two include modes (default isolation vs `allow_local_includes=true` running from the model's directory), that stdlib includes work in both modes, path validation (existence/is-file/non-empty/valid-UTF-8 model; paths resolved to absolute — prefer absolute paths), and the same result shapes as the inline tools.
- [ ] State the **security/read-scope posture**, truthfully: default isolation is **cwd-relative, not a sandbox** — it blocks *relative* local includes but an **absolute-path** include (or `../`-traversal) is still read, same as the inline tools; allow mode additionally reads files reachable via *relative* includes from the model's directory; either way still no network, no upload, no telemetry.
- [ ] In `find_unsat_core_files`, carry the existing **best-effort `core` / authoritative `stdout`**, model-entry-file-only caveat (allow-mode MUS members from included files stay in `stdout`).
- [ ] Note the inline tools remain for isolated/ephemeral text workflows.
- [ ] Verify: re-read the rendered section; no code to test.

### Task 7: Final `just check`

- [ ] `just check` — lint + typecheck + tests green.

## Acceptance criteria

- `check_minizinc_files`, `solve_minizinc_files`, `find_unsat_core_files` each appear in `mcp.list_tools()` with `{model_path, data_path, allow_local_includes}` (+ `solver` for solve/check, not findMUS) and return `CheckResult`/`SolveResult`/`UnsatCoreResult` respectively.
- **Isolated (default):** the run is isolated identically to the inline tools — argv model basename is `model.mzn` under a private temp dir, `cwd` is that temp dir (not the user's directory); a *relative* local include resolves against the empty temp dir and comes back as `status="error"` with MiniZinc's diagnostic in `stderr` (not raised, not pre-detected); a stdlib include still resolves; an absolute-path include is **not** blocked (cwd-relative isolation, documented).
- **Allow mode:** the managed binary runs on the resolved real `model_path` (data positional after it) with `cwd=model_path.parent`; relative includes resolve; findMUS structured `core` resolves entry-file spans by the real basename and excludes **differently named** included-file spans (which stay in `stdout`); a **duplicate-basename** collision is a known best-effort limitation (`stdout` is authoritative).
- Path validation resolves inputs to absolute and **returns** the resolved paths, and rejects missing / non-file `model_path`, missing `data_path`, an empty/whitespace-only model file, and a **non-UTF-8** model file with a clear `ValueError` → MCP error **before** any subprocess (the offending path is in the message); empty `model` and non-UTF-8 `model` raise in **both** modes; empty `data` is allowed. A relative input runs the resolved absolute path with `cwd` = its resolved parent (no subdir double-counting).
- Runtime-missing, non-positive `timeout_ms`, and OS exec failures surface as the same MCP errors / exception types as the inline tools.
- The inline tools (`solve_minizinc_model` / `check_minizinc_model` / `find_unsat_core`, backed by minizinc-layer `solve_model` / `check_model` / `find_unsat_core`) are **byte-identical** in behavior — the full pre-existing `tests/test_minizinc.py` and `tests/test_server.py` suites pass unchanged.
- Real-runtime `integration` tests prove: stdlib include works isolated, *relative* local include fails isolated, an **absolute-path** include still resolves under default isolation (the "cwd-relative, not a sandbox" contract), the same relative local include succeeds in allow mode, and findMUS allow-mode core resolves; `just integration` green where a runtime is installed.
- README documents the three tools, both include modes, path validation, the security/read-scope posture, and the model-only-core caveat.
- `just check` is green. No telemetry, no network calls, no LangChain/LangGraph, no bare-`$PATH` MiniZinc, no server-side LLM/sampling, no schema changes, no new global mutable state.

## Known risks

- **Expanded read scope in allow mode (by design).** `allow_local_includes=True` lets MiniZinc read any file reachable via includes from `model_path.parent`. This is the accepted product trade-off; it is default-off, gated behind an explicit flag, and documented. Not a bug.
- **Default isolation is cwd-relative, not a sandbox.** The default mode blocks *relative* local includes (they resolve against an empty private temp dir) but does **not** block an **absolute-path** include or `../`-traversal out of the temp dir — MiniZinc opens a complete-path include directly. This is identical to the inline tools' existing behavior and acceptable under the convenience-first product decision and the "local user, own files" threat model. The plan therefore **weakens the isolation claim** (truthful "relative includes only") rather than adding a fragile string-scanning guard that would contradict that decision; if strict refusal of absolute-path includes is later wanted, it is an additive guard, out of scope here.
- **No proactive local-include detection.** We deliberately do **not** pre-classify includes (stdlib vs local is unreliable statically). A model with a local include fails in the default mode with MiniZinc's own diagnostic; the agent learns the retry path from the tool description. If a reviewer wants a proactive scan, it can be added as a thin, non-gating hint later without changing this contract — but it is out of scope here.
- **UTF-8 assumption.** The path tools read `model_path`/`data_path` as UTF-8 (matching the inline tools' existing UTF-8 round-trip; MiniZinc source is conventionally UTF-8). A non-UTF-8 file is **not** an opaque traceback: reads go through `_read_text_utf8`, which wraps `UnicodeDecodeError` as `ValueError(<path>)` → MCP error, meeting the repo's "clear errors" bar. The model is decode-checked in both modes (validation); a non-UTF-8 *data* file is wrapped the same way in isolated mode and left to MiniZinc (`status="error"`) in allow mode.
- **findMUS filename coupling + basename collision.** Allow-mode core parsing depends on the real model basename matching the `Traces` token, and the filter is **basename-only** — an included file sharing the entry model's basename in another directory can be mis-attributed to `core`. Covered/pinned by unit tests (synthetic real basename + differently-named included span; plus a duplicate-basename test that documents the collision) and the integration test. This sits inside the tool's existing **best-effort `core` / authoritative `stdout`** contract; tighten to full-path matching only if Task 5 confirms findMUS emits resolvable full paths.
- **Refactor must preserve inline behavior.** Extracting `_invoke_minizinc` and the result builders must keep `cwd=str(tmp_dir)`, `encoding="utf-8"`, the `(timeout_ms/1000)+5` grace, and the `model.mzn`/positional-data shape exactly — the existing inline tests (command shape, cwd, timeout grace, utf-8) are the guard and must stay green throughout Task 1.
- **Relative paths.** A relative `model_path` resolves against the server process's cwd, which in MCP stdio is wherever the client launched the server — potentially surprising. Mitigated by resolving to absolute *before* validation/execution (validation returns the resolved paths) and documenting "prefer absolute paths".