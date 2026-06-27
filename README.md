# openconstraint-mcp

[![CI](https://github.com/KeLiu99/openconstraint-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/KeLiu99/openconstraint-mcp/actions/workflows/ci.yml)

A local-first [Model Context Protocol](https://modelcontextprotocol.io) server for
constraint programming and optimization. `openconstraint-mcp` gives an MCP client a
deterministic way to compile-check and solve [MiniZinc](https://www.minizinc.org/)
models on a **managed** solver runtime, exposing open-source solvers (OR-Tools CP-SAT
by default, Chuffed as an optional verifier) over MCP stdio.

Constraint problems — scheduling, rostering, assignment, routing, production
planning, inventory — are exactly where a language model is most likely to produce an
answer that looks right but is subtly infeasible. The division of labor here is
**LLM proposes, server verifies**: the client's LLM drafts a MiniZinc model, and the
local runtime compiles and solves it to produce a checked result. The server runs the
solver; it never drafts a model of its own and never calls an LLM.

Everything runs on your machine. No telemetry, no background network calls, and
nothing leaves your machine unless you opt in — the only network access in the entire
package is the runtime download you trigger explicitly with `install-runtime`.

## Design principles

- **Local-first.** Solving, validation, and result inspection all run on your machine.
  There are no remote solving backends and no upload of your models or data.
- **Managed runtime.** Solver execution always goes through a MiniZinc runtime this
  project resolves and controls, never an arbitrary `$PATH` binary — so a run does not
  depend on whatever MiniZinc happens to be installed on the host.
- **LLM proposes, server verifies.** Natural-language → model translation, critique,
  and repair belong in the MCP *client's* LLM. The server owns the deterministic half:
  compile-check, solve, and report the runtime's verbatim output. It holds no LLM
  credentials and never invokes a generative model.
- **No hidden network calls.** Validation, solving, and result inspection are all
  offline. The only sanctioned network call is the runtime download, and only when you
  run `install-runtime` — never on import, on server boot, or as a "convenience".
- **No telemetry.** Not implemented. Any future telemetry would be opt-in and
  documented.

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

## Quick start (MCP users)

After installing the package above:

1. **Set up MiniZinc** — one of:
   - `openconstraint-mcp install-runtime` to fetch and install the managed bundle (Linux x86_64, macOS arm64, Windows x86_64).
   - `openconstraint-mcp configure-runtime --runtime-dir <path>` to point the package at an existing MiniZinc install (a directory containing `bin/minizinc`).
2. **Verify:** `openconstraint-mcp check-runtime` and `openconstraint-mcp list-solvers`.
3. **Wire into your MCP client.** This repository includes `.mcp.json` for clients
   that read repo-local MCP config:

   ```json
   {
     "mcpServers": {
       "openconstraint": {
         "type": "stdio",
         "command": "uv",
         "args": ["run", "openconstraint-mcp", "stdio"]
       }
     }
   }
   ```

   For clients that use a global config instead, use the same stdio command from this
   checkout, or install the package on your `PATH` and run `openconstraint-mcp stdio`.
   Restart your MCP client; `check_runtime` and `list_available_solvers` tools should appear.

   Codex also reads the project-scoped `.codex/config.toml` in this checkout, so
   `openconstraint-mcp` is visible to Codex only while working in this repository.
   That config launches `uv run --no-sync openconstraint-mcp stdio` to avoid
   implicit dependency installs or network access on Codex startup; run
   `uv sync --all-groups` first if the server is not available.

## CLI

The package exposes five commands:

- **`openconstraint-mcp stdio`** — run the MCP server over stdio. This is the entry
  point an MCP client (e.g. Claude Desktop, Claude Code) launches.
- **`openconstraint-mcp install-runtime`** — fetch and install the managed
  MiniZinc bundle (Linux x86_64, macOS arm64, and Windows x86_64 in v0). Streams
  the pinned upstream asset from the MiniZinc GitHub release (a `.tgz` on Linux, a
  `.dmg` on macOS, the NSIS `setup-win64.exe` on Windows — run silently),
  verifies its SHA256, installs it into the chosen target, smoke-checks the
  resulting `bin/minizinc` (`bin\minizinc.exe` on Windows), and remembers the
  install location so `check-runtime` and `list-solvers` find it without further
  configuration. This is the **only** command in the package that touches the
  network.

  Flags:

  - `--runtime-dir <path>` — explicit install location. Overrides
    `OPENCONSTRAINT_MCP_RUNTIME_DIR`, the persisted install config, and the
    platformdirs default, and suppresses the interactive path prompt. Recommended
    when you want to be certain where the install lands.
  - `--yes` / `-y` — non-interactive: skip the path prompt **and** skip the
    overwrite-confirmation prompt **only for a prior managed install**. `--yes`
    is required for non-TTY (CI / scripted) runs.

    `--yes` does **not** force overwrite of an unmanaged non-empty directory.
    Pointing `--runtime-dir` at `$HOME`, `/tmp`, a project checkout, or any
    directory the installer did not previously write to is refused regardless of
    `--yes`. The marker file `.openconstraint-runtime.json` written into the
    runtime root is what makes a directory eligible for overwrite — `--yes`
    only authorises replacing the installer's own prior output.

  When stdin is a TTY and neither `--runtime-dir` nor `--yes` is given, the
  command prompts for the install location (Enter accepts the default).
- **`openconstraint-mcp configure-runtime --runtime-dir <path>`** — point the
  package at an existing MiniZinc install (e.g. a system install, package-manager
  install, or one you built yourself) without setting
  `OPENCONSTRAINT_MCP_RUNTIME_DIR`. Validates that `<path>/bin/minizinc` exists
  and is executable, then persists the path to the install config. Does not
  download anything and does not claim ownership of the directory — use this
  when you already have MiniZinc on disk and just want `openconstraint-mcp` to
  find it.
- **`openconstraint-mcp check-runtime`** — report whether the managed MiniZinc
  runtime is installed. Prints the expected runtime path and exits 0 when present,
  exits 1 otherwise.
- **`openconstraint-mcp list-solvers`** — list solvers exposed by the managed
  MiniZinc runtime. Requires the runtime to be installed; exits 1 with a clear
  error otherwise.

## MCP tools

The stdio server exposes two runtime-introspection tools, a model-check tool, a
model-inspection tool, an execution tool, an unsat-core diagnostic tool, and
background/portfolio job tools — each of the MiniZinc tools in an **inline-source**
form (below) and a **path-based file** sibling ([Path-based file tools](#path-based-file-tools)) — plus a
verified-save tool that persists a successful inline workflow to a local
project directory. The two solve
tools also accept optional solution checkers, so a normal solve can validate each
produced solution against a checker model without changing result shape:

- **`check_runtime`** — returns a `RuntimeStatus` with fields
  `installed: bool`, `runtime_dir: str`, and `minizinc_binary: str | None`.
- **`list_available_solvers`** — returns a `SolverList` of `SolverInfo` entries
  (`id`, `name`, `version`, `tags`, and a `capabilities` object), plus a
  top-level `capability_note`. `capabilities`
  carries `supports_all_solutions` (`-a`), `supports_free_search` (`-f`),
  `supports_parallel` (`-p`), `supports_random_seed` (`-r`),
  `supports_num_solutions` (`-n`), and an advisory `std_flags` list — deterministic
  facts read from the managed runtime's `--solvers-json` config for client-side
  solver routing. `supports_num_solutions` is the conservative gate
  (`org.gecode.gecode` / `org.chuffed.chuffed` only, matching the `num_solutions`
  solve control). The four `-a/-f/-p/-r` facts are **enforced** for the named
  controls they correspond to: a requested `all_solutions` / `free_search` /
  `parallel` / `random_seed` is rejected before solving when the selected solver's
  `stdFlags` omit the matching flag. Enforcement is by exact canonical solver `id`
  (the same stance as the `num_solutions` gate), so select non-default solvers by
  canonical id to get the upfront rejection — a short alias (e.g. `gecode`) or an
  unknown solver does not resolve and passes through to MiniZinc unchanged.
  `std_flags` stays advisory — it reports the standard flags the
  solver configuration declares and is **not** a passthrough, so clients cannot
  send those flags back into `solve_minizinc_model` / `solve_minizinc_files`.
  Alongside the structured `SolverList`, the tool returns model-visible text
  content presenting a complete `id`/`name`/`version` inventory table of
  **every** solver (with a final-answer requirement to copy the table without
  omitting rows, converting it to bullets/prose, summarizing, or grouping
  entries), followed by a user-visible note that detailed solver capabilities
  can be requested, a `num_solutions` routing note, and a caution that a declared
  MIP solver may still need separate binaries/licenses to run. The full
  `capabilities` metadata stays in the structured result and is not printed by
  default — request it explicitly to surface the `supports_*` booleans and
  `std_flags`. Raises a runtime-missing error if the managed MiniZinc binary is
  not present.
- **`check_minizinc_model`** — compile-check a complete MiniZinc model
  through the managed local runtime **without solving it**. This is the
  cheap pre-flight before `solve_minizinc_model`: it runs MiniZinc's
  dry-run compile (`-c`) for the chosen solver, flattening the model to
  FlatZinc but stopping before the search, so it catches syntax, type,
  missing-include, invalid-domain, and unsupported-construct errors in a
  fraction of a solve. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path. A
    parameterized model needs its data to flatten, so check it with the same
    `data` you intend to pass to `solve_minizinc_model`.
  - `solver: str = "cp-sat"` — passed through verbatim to MiniZinc's
    `--solver` flag. The compile is solver-aware, so a model that
    compiles for one solver may not for another — check against the
    solver you intend to solve with. An unknown or unavailable solver is
    a compile failure: it surfaces as `status="error"` with MiniZinc's
    diagnostic in `stderr`, not as an MCP error.
  - `timeout_ms: int = 30000` — compile budget in milliseconds, enforced
    as a wall-clock cap on the runtime subprocess (plus a few seconds'
    grace). It is also passed through to MiniZinc's `--time-limit`, but
    that flag primarily bounds *solving*, so for a compile the subprocess
    cap is the real stop. Must be strictly positive (`0` is a validation
    error, not "no timeout").

  Returns a `CheckResult` with fields:

  - `status: str` — one of `"ok"`, `"error"`, `"timeout"`. `"ok"` means
    **the model compiles, not that it is satisfiable** — compilation does
    not run the search, so a clean check does not guarantee a solution
    exists (that is only known after solving).
  - `solver: str` — the solver the model was flattened for, echoed from
    the request.
  - `stdout: str` — the runtime's raw stdout (normally empty on a clean
    compile).
  - `stderr: str` — the runtime's raw stderr (compile diagnostics and
    warnings land here).
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **Failure-mode contract.** As with `solve_minizinc_model`, environment
  and argument problems — runtime not installed, empty `model`,
  non-positive `timeout_ms`, OS-level failure to exec the managed binary —
  surface as **MCP errors**. Compile diagnostics come back as a normal
  `CheckResult` with `status="error"` and the diagnostic in `stderr`, so a
  client LLM can repair the model and re-check without exception handling.

  **Recommended loop.** `check_minizinc_model` is the validate step in
  **draft → check → repair → solve → explain**: draft a model, check it,
  repair on `status="error"` and re-check until `"ok"`, then hand the clean
  model to `solve_minizinc_model`. Validating first turns a class of
  failures into cheap compile errors instead of spent solve attempts. When
  the model uses inline data, pass the **same** `data` to both the check and
  the solve call so you validate and solve the same instance.

- **`inspect_minizinc_model`** — inspect a model's **interface without solving
  it**. It wraps the managed runtime's `--model-interface-only` flag, which runs
  MiniZinc's type analysis and stops *before* flattening or search, so it is even
  cheaper than `check_minizinc_model`. Use it to discover what data a model needs
  (so a client LLM can build a correct `.dzn`) and what it outputs, before
  spending a solve. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline `.dzn` data, written to a
    private temp file beside the model and passed as a positional data file
    (same contract as `check_minizinc_model`). Supplying data narrows the
    reported `required_parameters` (see below); omit it to see the model's full
    required set.
  - `solver: str = "cp-sat"` — passed through to `--solver`. Interface
    extraction is solver-independent in practice, but the flag is accepted for
    consistency with the other tools.
  - `timeout_ms: int = 30000` — wall-clock budget (must be strictly positive);
    shares the `check` default, since inspection is a comparable pre-flight.

  Returns a `ModelInspectionResult` with fields:

  - `status: str` — one of `"ok"`, `"error"`, `"timeout"`. **`"ok"` means only
    that the interface was *extracted* — it is NOT a data-completeness signal.**
    A no-data inspection is `"ok"` with a *non-empty* `required_parameters`
    (that is the whole point of the tool). Completeness is signalled solely by
    `required_parameters == {}`.
  - `solver: str` — echoed from the request.
  - `interface: ModelInterface | None` — populated **only when `status="ok"`**,
    with fields:
    - `method: str` — the solve kind, one of `"sat"`, `"min"`, `"max"`.
    - `required_parameters: dict[str, InterfaceType]` — the parameters **still
      needing a value** given any `data` you passed. With no data this is the
      model's full required set; supplying the matching data shrinks it to `{}`.
    - `output_variables: dict[str, InterfaceType]` — the model's output variables.
      **Advisory:** with an `output` item this tracks the output-referenced
      variables and excludes functionally-defined ones, so treat it as "the
      model's output variables", not "every decision variable".
    - `has_output_item: bool` — whether the model declares an `output` item.
    - `globals: list[str]`, `included_files: list[str]` — as reported by the
      runtime.

    Each `InterfaceType` carries `base_type` (one of `"int"`, `"bool"`,
    `"float"`, `"string"`, `"tuple"`, `"record"`, `"ann"`), `dim` (array
    dimensionality; `0` for a scalar), `is_set` (`true` for a set type), and
    `is_optional` (`true` for an `opt` type). `"ann"` is MiniZinc's annotation
    type — e.g. an `array[1..2] of ann` search-strategy list passed to
    `seq_search`. **This mode does not surface:** enum-typed entries appear as
    `base_type="int"` (enum names are not exposed — infer them from the model
    text); variable domains and parameter ranges (e.g. `1..n`) are not reported;
    array index sets are not reported, only the `dim` count; and `tuple`/`record`
    entries carry only the tag, not their component types.
  - `stdout: str` / `stderr: str` — the runtime's raw output. A *successful*
    inspection may still emit warnings to `stderr`, so `status="ok"` does not
    depend on empty `stderr`.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **Failure-mode contract.** Identical to `check_minizinc_model`: environment and
  argument problems (runtime missing, empty `model`, non-positive `timeout_ms`,
  OS-level exec failure) surface as **MCP errors**; a model type/syntax error
  comes back as a normal `ModelInspectionResult` with `status="error"`,
  `interface=None`, and the diagnostic in `stderr`.

- **`solve_minizinc_model`** — run a complete MiniZinc model through the
  managed local runtime. Arguments:

  - `model: str` — the complete MiniZinc source (declarations, constraints,
    exactly one `solve` statement, and an `output` block). Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path.
  - `checker: str | None = None` — optional inline MiniZinc checker source,
    written beside the model as `checker.mzc.mzn` and passed through
    MiniZinc's `--solution-checker` flag. Omit it for an ordinary solve.
  - `solver: str = "cp-sat"` — passed through verbatim to MiniZinc's
    `--solver` flag.
  - `timeout_ms: int = 30000` — solving budget in milliseconds. Must be
    strictly positive. `0` is **not** "no timeout" — it is a validation
    error. Pass a real budget, or omit the argument to get the default.
  - `free_search: bool = False` — when true, passes `-f`: the solver may
    ignore the model's search annotations and use its own search strategy.
    This means "search freely", **not** "no search"; its effect is
    solver-dependent (large for Chuffed's LCG, often minor for CP-SAT).
  - `parallel: int | None = None` — when set, passes `-p <n>` to request `n`
    parallel search threads. Must be `>= 1`.
  - `random_seed: int | None = None` — when set, passes `-r <n>` to seed the
    solver's randomization. Any int is accepted.
  - `all_solutions: bool = False` — when true, passes `-a`: enumerate every
    solution (satisfaction) or the optimization improving-sequence, all
    captured in order in `solutions`.
  - These four `-a/-f/-p/-r` controls are **capability-gated**: if the selected
    solver's runtime-local `stdFlags` (see `list_available_solvers`) do not
    declare the matching flag, the request is rejected **before solving** with an
    actionable error naming the solver, the control, and the flag. The check
    matches the solver by exact canonical `id`; a short alias (e.g. `gecode`) or
    an unknown solver does not resolve and passes through to MiniZinc unchanged.
  - `num_solutions: int | None = None` — when set, passes `-n <n>` to cap the
    number of solutions for a **satisfaction** problem. Must be `>= 1`. It is
    **solver-gated**: only `org.gecode.gecode` and `org.chuffed.chuffed`
    support `-n`; the default `cp-sat` (and any other solver) returns a clear,
    actionable error instead of a broken run. It is **not** meaningful for
    optimization (`minimize`/`maximize`) — use `all_solutions` there for the
    improving sequence. For multiple optimal solutions, first solve the
    optimization to a proven optimum, then re-solve as a satisfaction model
    with the objective fixed to that value and use a supported
    `num_solutions` solver.

  All five search controls are optional and **solve-only** (not on the check
  or findMUS tools); with none set, the invocation is byte-identical to the
  default solve.

  Returns a `SolveResult` with fields:

  - `status: str` — one of `"timeout"`, `"error"`, `"unsatisfiable"`,
    `"unbounded"`, `"unsat_or_unbounded"`, `"unknown"`, `"optimal"`,
    `"satisfied"` (precedence in that order — see the source for details).
  - `solver: str` — the solver name that ran, echoed from the request.
  - `return_code: int | None` — the managed binary's subprocess return code,
    or `null` when the outer subprocess timeout fired before a real return
    code existed (so `null` on `status="timeout"`).
  - `timed_out: bool` — `true` when the subprocess wall-clock cap fired. This
    is explicit process-timeout metadata; today it is redundant with
    `status="timeout"`, not a new independent solver signal.
  - `stdout: str` — the human-readable solution text, **reconstructed** from
    the solve stream's `default` output sections (one solution's `output`
    block per block). When a model declares no explicit `output` item the
    stream carries only the `json` section, so each solution's block is instead
    synthesized as `name = <value>;` lines from its variable map (objective
    excluded) — the solution is shown either way. Solve runs use MiniZinc's
    `--json-stream` transport, so this is the rendered solution text, not the
    literal process bytes (which are line-delimited JSON); when no checker is
    supplied, the raw stream is not surfaced.
  - `stderr: str` — the run's **diagnostic channel**: the managed process's
    real stderr plus any solve-stream `error`/`warning` messages folded in
    (deduplicated). `--json-stream` may route model/solver diagnostics into
    the stdout stream as error objects, so they are collected here regardless
    of channel — read `stderr` for what went wrong.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.
  - `solution: dict[str, Any] | None` — the best/last solution as a
    variable-name → value map (the stream's `json` section, model variables
    only; the objective is reported separately, not folded in). `null` when
    no solution was produced.
  - `solutions: list[dict[str, Any]]` — every emitted solution in order (the
    optimization improving-sequence, or an `all_solutions` enumeration). Its
    last entry is `solution`; `[]` when none.
  - `objective: int | float | None` — the best objective, taken from the last
    solution. `null` for pure-satisfaction problems and when no solution was
    produced.
  - `statistics: dict[str, str]` — best-effort solver statistics, merged from
    the stream's `statistics` objects (typed values stringified, last-wins on
    duplicate keys). May be `{}` when none were emitted; the key set is
    solver- and version-defined, **not** a stable contract. Unlike the prior
    stdout scrape, these are **driver-emitted** sibling stream objects, so a
    model's `output` block can no longer forge them.
  - `checker: CheckerReport | None` — `null` unless a checker was supplied.
    When present, it carries:
    - `status: str` — one of `"completed"`, `"violation"`, `"no_solution"`,
      `"error"`, `"timeout"`.
    - `checks: list[SolutionCheck]` — one checker verdict per produced solution,
      index-aligned with `solutions` when checking completed or found a
      violation. Each entry has `violation: bool` and `output: str`.
    - `transcript: str` — the authoritative raw `--json-stream` transcript,
      including both solve and checker objects. `stdout` remains the
      reconstructed solution text only.

  **Solution checking.** Checking augments a normal solve: it adds exactly
  `--solution-checker` to the same managed MiniZinc invocation, so `free_search`,
  `parallel`, `random_seed`, `all_solutions`, and supported `num_solutions` all
  compose with it. A checker's `CORRECT`/`INCORRECT` text is surfaced verbatim in
  `checker.checks[].output` and is **not** interpreted by the server; only a
  nested `UNSATISFIABLE` makes `checker.status="violation"`. Rejected solutions
  still appear in `solutions`, so consult the aligned checks before treating each
  produced solution as valid. A checker validates solution correctness and can
  recompute an objective, but it never proves optimality — `status` remains the
  completeness/optimality signal.

  Inline checkers run in the same private temp directory as the inline model, so
  they may include the co-located `model.mzn` but cannot resolve arbitrary
  project-relative local includes. For multi-file checker projects, use
  `solve_minizinc_files` with `checker_path`.

  The MCP response also includes model-visible text content with status,
  solver metadata, stdout/stderr, and a `Statistics:` section whenever
  the parsed `statistics` map is non-empty. That text includes an explicit
  final-answer requirement telling the client's LLM not to omit the section.
  `structuredContent` still carries the complete validated `SolveResult` for
  clients that consume structured output directly.

  **Division of labor.** The `solve_constraint_problem` MCP prompt (below)
  guides the client LLM to draft a MiniZinc model; `solve_minizinc_model`
  executes that drafted model locally and returns the runtime's verbatim
  output. `LLM proposes, server verifies.`

  **Failure-mode contract.** Environment and argument problems —
  runtime not installed, empty `model`, non-positive `timeout_ms`, OS-level
  failure to exec the managed binary — surface as **MCP errors** the
  client must surface to the user. Solving outcomes — unsat, unbounded,
  timeout, MiniZinc model/syntax/type/solver errors — come back as a
  normal `SolveResult` whose `status` field encodes the outcome, so a
  client LLM can branch on it (and feed `stderr` back into a revise-and-
  retry loop) without exception handling.

- **`find_unsat_core`** — diagnose why a MiniZinc model is unsatisfiable by
  wrapping findMUS (`org.minizinc.findmus`) through the managed runtime.
  This complements the solve loop: when `solve_minizinc_model` returns
  `status="unsatisfiable"`, call `find_unsat_core` to localize the conflict.
  Pass the **same** `data` you passed to that solve: a parameterized model
  needs it to flatten at all, and diagnosing a different instance than the
  one that proved unsat is meaningless. Arguments:

  - `model: str` — the complete MiniZinc source. Must not be empty.
  - `data: str | None = None` — optional inline MiniZinc data (`.dzn`
    contents — any data assignments, not parameter-only) provided directly
    as text; omit (or pass `null`) for models that need no external data.
    It is written to a private temp file alongside the model and passed to
    the managed runtime as a positional `.dzn` data file (MiniZinc's
    `model.mzn data.dzn` order) — never a client-supplied path.
  - `timeout_ms: int = 30000` — findMUS budget in milliseconds. Must be
    strictly positive. `0` is a validation error, not "no timeout".

  Returns an `UnsatCoreResult` with fields:

  - `status: str` — one of `"mus_found"`, `"no_core"`, `"error"`,
    `"timeout"`. Clients branch on this field; there is no derived
    `core_found` flag.
  - `core: list[UnsatCoreConstraint]` — best-effort structured constraints
    from the submitted model, each with `line`, `column`, `end_line`,
    `end_column`, and `source`. This may be empty even when a MUS was found.
  - `message: str` — short run-specific summary.
  - `stdout: str` — raw findMUS output, preserved verbatim and authoritative.
  - `stderr: str` — raw runtime diagnostics.
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.

  **MUS caveat.** The tool reports **a** minimal unsatisfiable subset:
  constraints that are jointly unsatisfiable and from which none can be
  removed while staying unsatisfiable. Minimal does **not** mean globally
  smallest, and a model may have several MUSes.

  **Model-only `core`.** The structured `core` is **best-effort** and
  resolves **model-file** spans only; raw `stdout` is authoritative. A
  `.dzn` cannot contain `constraint` items, but assigning a *decision
  variable* in data is equivalent to a constraint, so if the client does
  that, a MUS member can originate in the data file — it appears in raw
  `stdout` but is **not** added to `core`. Do not treat `core` as a
  complete enumeration of the conflict.

  **Conservative `no_core`.** `status="no_core"` means findMUS completed
  without reporting a MUS, **not** that the model is satisfiable. A tight
  `timeout_ms` can also surface as `no_core` rather than `timeout` if
  findMUS stops at its own `--time-limit` with return code 0.

  **Failure-mode contract.** Environment and argument problems — runtime not
  installed, empty `model`, non-positive `timeout_ms`, OS-level failure to
  exec the managed binary — surface as **MCP errors**. findMUS outcomes —
  MUS found, no MUS reported, findMUS/runtime diagnostics, and timeout — come
  back as a normal `UnsatCoreResult` whose `status` encodes the outcome.

- **`save_verified_minizinc_model`** — persist a *successful* inline MiniZinc
  workflow to a local project directory, **after the server re-verifies it**
  through the managed runtime. The inline tools above are ephemeral by design:
  a model that checked and solved exists only in the conversation. This tool
  turns that result into a durable local project — without trusting the
  client's claim that the model worked. Arguments:

  - `model: str` — the complete MiniZinc source to verify and save.
  - `target_dir: str` — **explicit absolute path** of the directory to create
    or update; its parent must already exist. The server opens **no OS file
    dialog or picker** — choosing the path is the client's job (ask the user,
    or use the client's own UI), and the chosen path is passed here. MCP
    elicitation is deliberately **not** used or required in v1; the explicit
    `target_dir` argument is the durable contract that works in every client.
  - `data: str | None = None`, `checker: str | None = None` — optional inline
    `.dzn` data and solution-checker source, with the same semantics as
    `solve_minizinc_model`; the re-check and re-solve both use them.
  - `problem: str | None = None` — the user's original natural-language
    problem text. Saved only when passed explicitly; the server never infers
    or retains conversation history.
  - `solver`, `timeout_ms`, `free_search`, `parallel`, `random_seed`,
    `all_solutions`, `num_solutions` — the same solve controls as
    `solve_minizinc_model`, applied to the verifying solve and recorded in
    the manifest so the recorded verification is reproducible.
  - `overwrite: bool = False` — required to replace a previous save (see the
    overwrite gate below).

  **Verification gate.** Before anything is written, the server re-runs the
  compile check and then the solve on the artifacts exactly as supplied. The
  save proceeds only when the check is `"ok"` **and** the solve finished
  `"satisfied"` or `"optimal"` with a clean exit and no timeout **and** —
  when a `checker` is supplied — the nested checker report is `"completed"`
  (the checker ran without machine-readable violation; **not** a proof of
  optimality). Any other outcome returns `status="not_verified"` carrying the
  gating `check`/`solve` results and writes **nothing**.

  **Artifact layout.** The saved directory uses fixed filenames — the only
  user-chosen path is the directory itself:

  | File | Written | Contents |
  | --- | --- | --- |
  | `model.mzn` | always | the verified model source, verbatim |
  | `data.dzn` | only when `data` was passed | the `.dzn` text (may be empty) |
  | `checker.mzc.mzn` | only when `checker` was passed | the checker source |
  | `problem.md` | only when `problem` was passed | the original problem text |
  | `solve-result.json` | always | the verifying `SolveResult` as JSON |
  | `.openconstraint-model.json` | always | manifest: tool version, timestamp, solver, the solve controls used, a verification summary, and per-file sha256 hashes |

  **Overwrite safety (marker-gated).** A brand-new path or an existing empty
  directory is written directly. A non-empty directory is replaced only when
  *all three* hold: it contains a prior save's `.openconstraint-model.json`
  manifest, `overwrite=true` was passed, and it holds no files the prior save
  did not write. Anything else — user files present, an unrecognizable
  manifest, a missing `overwrite` — is refused with an actionable MCP error
  before any solver runs. Replacement is wholesale, via a staged hidden
  sibling directory and atomic rename swap (restoring the prior directory
  from its backup if the swap itself fails), so a save can never leave a
  half-written directory or a stale file from an earlier save behind.

  Returns a `SaveVerifiedModelResult`: `status` (`"saved"` /
  `"not_verified"`), `message`, the resolved `target_dir` (echoed on both
  outcomes; on `not_verified` it names the directory that was *not* written),
  `files` (role, bare filename, and sha256 per saved file — empty unless
  `saved`), `check` (always present), and `solve` (`null` when the check gate
  already failed). The save runs entirely locally: no network, no LLM, no
  telemetry — and it writes only inside (and, transiently while staging,
  beside) the explicit `target_dir`.

### Background solve jobs

`solve_minizinc_model` blocks until the solve finishes, which a hard problem
can outrun a client's synchronous request timeout. The job tools run the same
inline solve as a **background job**: submit returns immediately with a
`job_id`, and the client polls for the result on its own schedule. The job
registry is **in-process and ephemeral** — jobs do not survive a server
restart — and runs entirely locally through the managed runtime (no network,
no LLM, no telemetry).

- **`submit_solve_job`** — admit a solve as a background job. Takes the same
  inline surface as `solve_minizinc_model` (`model`, optional `data`/`checker`,
  `solver`, `timeout_ms`, and the `free_search` / `parallel` / `random_seed` /
  `all_solutions` / `num_solutions` controls). Argument errors (empty model,
  non-positive timeout, a bad `parallel`/`num_solutions`) are reported
  synchronously **before any job exists**. Returns a `SolveJobStatus` with a
  server-generated opaque `job_id` and an initial `state` of `"queued"` or
  `"running"`. Admission is **bounded**: at most a fixed number of jobs run at
  once, further submits sit `"queued"` up to a fixed cap, and a submit beyond
  that is **rejected with an MCP error** (retry once a running job finishes)
  rather than growing the queue unboundedly.
- **`get_solve_job`** — poll a job by `job_id`. This is the OS-independent way
  to watch a background solve — no `ps`/`Get-Process` needed. Returns the
  `SolveJobStatus`: `state` (`"queued"`, `"running"`, `"succeeded"`,
  `"failed"`, `"timeout"`, `"cancelled"`), `timeout_ms` (the requested solve
  time-limit, echoed in every state), timing fields, an optional `result` (the
  full `SolveResult`), and an optional `message`. **State contract:** `result`
  is present exactly when `state` is `"succeeded"` or `"timeout"`, so
  `state == "failed"` **iff** `result is None`. `"failed"` means the job
  machinery itself raised (see `message`); a *solver*-level `error` verdict is a
  `"succeeded"` job whose `result.status == "error"`, **not** `"failed"`. A
  `"timeout"` job still carries its partial `SolveResult`. While a job is
  `"running"`, only `state` and `elapsed_ms` advance — live mid-solve
  statistics are not provided, so **pace your polling against the job's own
  budget** (`remaining ≈ timeout_ms - elapsed_ms`, usually terminal shortly
  after that) rather than a fixed `sleep`: tight loops just burn calls since a
  `running` job exposes no new data between polls. A completed `"succeeded"` or
  `"timeout"` job is the only place a background solve's statistics surface —
  its `result.statistics` carries the same model-visible `Statistics:` section
  the synchronous solve tools produce.
- **`cancel_solve_job`** — request cancellation by `job_id`. A still-`queued`
  job is dropped before it starts; a `running` job has its managed MiniZinc
  **process tree** (solver children included) terminated. Cancellation is
  best-effort and idempotent: cancelling an already-terminal job is a no-op.
  The job reaches `"cancelled"` (with `result is None`); poll `get_solve_job`
  to confirm.
- **`list_solve_jobs`** — list the currently retained jobs, one
  `SolveJobStatus` per job. Finished jobs are retained only up to a cap, so the
  oldest terminal jobs may have been evicted.

These four tools return at once, so — unlike the blocking solve/check/inspect
tools — they emit no progress/log status notifications; watch a job's `state`
via `get_solve_job` instead. An unknown `job_id` is an MCP error.

### Solver portfolios

Race several **model formulations**, solvers, and seeds against **one** instance
and return the single winner. This is a **local race** over the same
managed-runtime background-solve machinery — there is no remote/distributed
solving, upload, or telemetry; every attempt runs on this machine. Reach for it on
a hard instance where the best formulation or solver is unknown; an ordinary
single-solver `solve_minizinc_model` is still the right first attempt.

Because a hard race can run past a client's synchronous request timeout, a
portfolio runs as a **background job**: submit it with
[`submit_portfolio_job`](#background-portfolio-jobs) and poll
[`get_portfolio_job`](#background-portfolio-jobs) for the winner. It takes the same
inline surface as `solve_minizinc_model` — optional shared `data`/`checker`, and
the non-seed controls `free_search` / `parallel` / `all_solutions` /
`num_solutions`, applied identically to every attempt — but takes a non-empty
**`models`** list (alternative encodings of the same instance, sharing the one
`data`/`checker`; **not** a batch solve of different problems) and a non-empty
`solvers` list instead of one `model`/`solver`, and **does not** take
`random_seed`. The portfolio API still exposes named controls only: there is no
generic `solver_options`, `extra_args`, or raw MiniZinc flag passthrough.

- **Seeds.** `seed_count` (default `1`) generates seeds deterministically: with
  `seed_count == 1` each `(model, solver)` runs once **unseeded**; with
  `seed_count > 1` each runs with seeds `1..seed_count`, so every selected solver
  must support `-r`. Use `seeds` for exact user-controlled values instead:
  `seeds=[42, 123, 999]` runs exactly those seeds, in that order, with no extra
  unseeded attempt. An explicit `seeds` list must be non-empty, must not contain
  duplicates, requires `seed_count` to stay at its default `1`, and still requires
  every selected solver to support `-r`.
- **Cross-product, no cap.** The plan is the full cross-product
  `len(models) * len(solvers) * seed_count` when using the shorthand, or
  `len(models) * len(solvers) * len(seeds)` when `seeds` is supplied, with the
  **model index varying fastest** so the first attempts span distinct formulations.
  There is **no portfolio-side cap**: every attempt is admitted; up to `max_running_jobs`
  (default `4`) race simultaneously and the rest **queue**, starting as running
  slots free, and a decisive running winner cancels the still-queued attempts
  before they start. The only breadth bound is the registry's running+queued
  capacity — a plan past it is rejected by the job registry (raise capacity via
  the [registry-bound env vars](#configuring-registry-bounds)). Unsupported
  `-a/-f/-p/-r` controls are rejected up front too (canonical-id match, like the
  single-solve gate). Mind plan size: the cross-product grows fast.
- **Winner policy.** The first attempt to reach a decisive verdict
  (`optimal`/`satisfied`/`unsatisfiable`/`unbounded`/`unsat_or_unbounded`) wins
  and the remaining attempts are **cancelled**; if none is decisive, the best
  available terminal attempt is returned (a timeout/error *with* a solution,
  then `unknown`, then a timeout without a solution, then an error).
- **Result.** A `PortfolioSolveResult`: `status` (`"winner"`/`"no_winner"`),
  `winner_index`, the winning `SolveResult` in `winner` (its own `status` tells
  you whether the win was decisive), `attempts` (every attempt's `model_index`,
  solver, seed, final state, result status, objective, and message — including
  the cancelled losers, so you need not poll child jobs), `elapsed_ms`, and
  `selection_policy`. The winning formulation is
  `models[attempts[winner_index].model_index]`. Present it like a single
  `solve_minizinc_model`: lead with the winner's model/solver/seed/status and then
  the winning solve.

### Background portfolio jobs

Portfolios run as background jobs — the portfolio analogue of
`submit_solve_job`/`get_solve_job`: submit the race and return immediately, then
poll for the winner, so a hard race never blocks past a client's synchronous
request timeout.

The design is **collect-on-poll**: there is no extra worker pool. The attempts
are admitted as ordinary jobs on the **same** solve registry as
`submit_solve_job` (so they count against its capacity and also show up in
`list_solve_jobs`), and winner-selection — the pure function of the attempts'
statuses — runs **when you call `get_portfolio_job`**. That keeps submit
non-blocking without cloning the job machinery.

- **`submit_portfolio_job`** — admit a portfolio race as a background job. Takes
  `models`, `solvers`, optional shared `data`/`checker`, `seed_count`, `seeds`,
  `per_attempt_timeout_ms`, and the non-seed controls (see
  [Solver portfolios](#solver-portfolios) above). Validation, capability
  enforcement, and admission run
  **synchronously**: an empty `models`/`solvers`, a bad control, an unsupported
  `-a/-f/-p/-r` flag, or a plan past the registry's running+queued capacity is
  reported at once as an MCP error, **before any job exists**. Returns a
  `PortfolioJobStatus` with an opaque `job_id` and `state` `"running"`.
- **`get_portfolio_job`** — poll a portfolio job by `job_id`. **Each poll drives
  the race**: once an attempt reaches a decisive verdict it selects the winner
  and cancels the still-running losers, so poll until terminal rather than
  submitting and walking away. Returns a `PortfolioJobStatus`: `state`
  (`"running"`, `"succeeded"`, `"cancelled"`), `per_attempt_timeout_ms`, timing
  fields, an optional `result` (the full `PortfolioSolveResult`), and an optional
  `message`. **State contract:** `result` is present exactly when `state` is
  `"succeeded"`. A race with no decisive winner is still `"succeeded"` (carrying a
  `"no_winner"` `PortfolioSolveResult`); a per-attempt failure is recorded in that
  result's attempts table, not as a failed job. Pace polling against
  `per_attempt_timeout_ms` rather than a fixed `sleep`.
- **`cancel_portfolio_job`** — stop a running race and **every** still-running
  attempt (each attempt's managed process tree is terminated). Best-effort and
  idempotent; the job reaches `"cancelled"` (with `result is None`).
- **`list_portfolio_jobs`** — list the retained portfolio jobs, one
  `PortfolioJobStatus` each. Finished jobs are retained only up to a cap.

Loser attempts are cancelled at the next poll (not the instant a winner appears),
bounded by each attempt's own `per_attempt_timeout_ms` — negligible for a polling
client, and the trade for not running a second worker pool. Like the other job
tools these return at once and emit no progress notifications; watch `state` via
`get_portfolio_job`. An unknown `job_id` is an MCP error.

### Configuring registry bounds

Background solve jobs (`submit_solve_job`) and a background portfolio job's
attempts share one in-process **job registry** with three bounds. They default to the values below and are overridable via environment
variables read **once at server start**:

| Env var | Meaning | Default | Minimum |
| --- | --- | --- | --- |
| `OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS` | Solves running concurrently | `4` | `1` |
| `OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS` | Submissions queued past the running cap | `16` | `0` |
| `OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL` | Finished jobs kept for status polling | `64` | `1` |

A submission (or portfolio batch) beyond the `running + queued` capacity is
rejected with a clear error. An **invalid** value — non-integer or below the
variable's minimum — **fails fast at server start, naming the offending variable**
(no silent fallback to the default). Raise `OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS` /
`OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS` to admit wider portfolios.

### Path-based file tools

The four tools above take the model (and optional data) as **inline source
text**, which the server writes to a private temp file. That is ideal for the
small/medium models a client LLM drafts, but it forces the agent to read an
entire `.mzn`/`.dzn` from disk and thread the whole contents through MCP
arguments. For large local models the server also exposes **path-based**
siblings that read the model/data from local file paths instead:

- **`check_minizinc_files`** — path-based sibling of `check_minizinc_model`.
- **`inspect_minizinc_files`** — path-based sibling of `inspect_minizinc_model`.
- **`solve_minizinc_files`** — path-based sibling of `solve_minizinc_model`.
- **`find_unsat_core_files`** — path-based sibling of `find_unsat_core`.

Each returns the **same** result shape as its inline counterpart
(`CheckResult` / `ModelInspectionResult` / `SolveResult` / `UnsatCoreResult`).
A path-based inspection is the one that genuinely benefits from running in the
model's own directory: the interface parses without data, but a relative
`include` must still resolve from the model's own dir. The inline tools are
unchanged and remain the right choice for ephemeral, isolated text workflows.

**Arguments** (all four):

- `model_path: str` — path to a local `.mzn` file on the machine running the
  server. Required; must exist and be a regular file.
- `data_path: str | None = None` — path to a local `.dzn` file, or `null`. An
  empty data file is allowed (a valid "no parameters" input).
- `checker_path: str | None = None` — `solve_minizinc_files` only. Optional
  path to a MiniZinc checker whose filename must end in `.mzc` or `.mzc.mzn`;
  it is resolved to absolute and validated before any run.
- `solver: str = "cp-sat"` — `solve`/`check`/`inspect` only (not
  `find_unsat_core_files`, which always uses findMUS).
- `timeout_ms: int = 30000` — same semantics as the inline tools; must be
  strictly positive.

`solve_minizinc_files` additionally accepts the same optional, solve-only
search controls as `solve_minizinc_model` — `free_search`, `parallel`,
`random_seed`, `all_solutions`, and the solver-gated, satisfaction-only
`num_solutions` (see above for semantics and defaults) — plus `checker_path`
for solution checking.

**Includes (MiniZinc CLI style).** The file tools run the managed binary on the
real `model_path` with the working directory set to the model's own directory,
exactly like running `minizinc` by hand. A **relative** include such as
`include "helpers.mzn";` therefore resolves against the model's directory, and
**standard-library** includes (`globals.mzn`, `alldifferent.mzn`, etc.) resolve
from the solver's library path. (The inline tools, by contrast, run the inline
source in a private temp dir, so relative local includes do not resolve
there — which is why the file tools exist.)

**Path validation.** Before any subprocess, each tool resolves `model_path` and
`data_path` to **absolute** paths (`Path.resolve()`, following symlinks the
caller named) and rejects, as a clear MCP error naming the offending path: a
missing or non-file `model_path`/`data_path`, an empty/whitespace-only model
file, and a non-UTF-8 model file. Relative inputs resolve against the server
process's working directory, which in MCP stdio is wherever the client launched
the server — **prefer absolute paths** to avoid surprises.

**Read scope.** A file tool reads the model file, the optional data file, and
any local files they reference through MiniZinc `include`. It does **not** write
files, make network calls, upload data, or use a remote solver, and solving
still goes through the managed runtime. The threat model is "a local user
pointing the tool at their own files": the tool reads nothing the user could not
read by hand.

**`find_unsat_core_files` core caveat.** As with the inline `find_unsat_core`,
the structured `core` is **best-effort** and `stdout` is **authoritative**. The
`core` resolves spans from the **entry model file only**: a MUS member that
lives in an *included* file appears in `stdout` but not in `core`. The
entry-file filter matches on **basename**, so an included file that shares the
entry model's basename in a different directory could have its spans
mis-attributed to the entry model — a documented limitation of the best-effort
core (raw `stdout` stays authoritative).

[License](#license) · 

### Progress and status notifications

The nine long-running tools (`check_minizinc_model` / `check_minizinc_files`,
`inspect_minizinc_model` / `inspect_minizinc_files`, `solve_minizinc_model` /
`solve_minizinc_files`, `find_unsat_core` / `find_unsat_core_files`, and
`save_verified_minizinc_model`) emit
status feedback while MiniZinc is running, on two MCP channels:

- **Progress notifications** (`notifications/progress`) are sent only when the
  client requests them by including `_meta.progressToken` in the tool-call
  request. Values are small increasing stage counters (`1` validating, `2`
  solver running, `3` parsing, `4` complete) with a short message; `total` is
  deliberately omitted. They are **status updates, not a solver completion
  percentage** — MiniZinc/CP-SAT expose no reliable cross-solver progress
  signal, so render them as a spinner, stepper, or status text, never as a
  determinate percent bar.
- **Log notifications** (`notifications/message`, level `info`) carry the same
  milestone messages and are sent for every request, no token required — so
  clients that surface MCP server logs always show activity state.

The MiniZinc subprocess runs in a worker thread, so both channels are
delivered while the solve is still in flight and the server stays responsive
to other requests during long runs. Both channels are local protocol messages
to the connected client; nothing changes in any tool's input schema, output
schema, or result semantics, and a client that supports neither channel simply
sees the final result as before.

## CP-SAT Python execution path

In addition to the MiniZinc declarative path, `openconstraint-mcp` exposes a
second solving path: the client's LLM writes OR-Tools CP-SAT Python, and the
server runs it in a **local child process**.

### Tools

- **`run_cpsat_python(source: str, timeout_ms: int = 30000)`** — execute
  LLM-generated OR-Tools CP-SAT Python source in a bounded child process and
  return a `CpsatPythonResult`. The script must emit a single JSON object as
  its last stdout line:

  ```json
  {"status": "optimal", "objective": 42.0, "solution": {"x": 3, "y": 7}}
  ```

  Valid `status` values: `optimal`, `feasible`, `infeasible`, `unknown`,
  `error`. Use the `solve_cpsat_python` prompt to generate conforming scripts.

  The child process runs under the server's own Python interpreter (the
  project venv, which already ships `ortools`), launched unbuffered (`-u`).
  Output beyond 1 MB is truncated and the child killed. Returns
  `CpsatPythonResult`: `status`, `solution`, `objective`, `stdout`, `stderr`,
  `return_code` (null on timeout), `timed_out`, `truncated`, `duration_ms`.

  **Partial result on timeout.** A long or optimization run can also print an
  intermediate JSON object of the same shape on each improved solution (from a
  `cp_model.CpSolverSolutionCallback`). Because the child is unbuffered, the
  last such block survives the timeout kill: on `status="timeout"` the
  server recovers it into `solution`/`objective` as the best-so-far (unproven —
  treat as feasible, not optimal), or leaves them null if none was printed in
  time. On a clean run the final block (printed after `Solve` returns) is the
  authoritative result.

- **`run_cpsat_python_file(script_path: str, timeout_ms: int = 30000)`** —
  path-based sibling of `run_cpsat_python`. Pass a local `.py` path instead of
  pasting the source, so iterating on a file does not mean re-copying it on
  every call. The script runs with its working directory set to the file's own
  directory, so a relative `open()` of a sibling data file or `import` of a
  helper module resolves (mirroring `solve_minizinc_files`). `script_path` is
  resolved to absolute and validated before any run — a missing path, a
  non-file, an empty/whitespace-only script, or non-UTF-8 content is rejected
  with a clear error and nothing runs. Same JSON output contract, output cap,
  timeout, tree-kill, and `CpsatPythonResult` shape (including timeout partial
  recovery) as `run_cpsat_python`.

- **`save_verified_cpsat_python(source, target_dir, problem=None, timeout_ms=…, overwrite=False)`**
  — re-run `source` and persist it to a local directory only when the re-run
  yields a verified solution (`status` in `optimal`/`feasible` AND a non-null
  `solution`). `target_dir` must be an explicit absolute local path; the
  server never opens a file dialog. Fixed filenames: `solution.py`,
  optionally `problem.txt`, and a `.openconstraint-model.json` manifest.
  Overwrite is marker-gated: a non-empty directory is replaced wholesale
  only when it holds a prior save's manifest, `overwrite=true` is passed,
  and it contains no untracked files. Returns `SaveVerifiedPythonResult`
  with `saved` (bool, derived from `status`), file list, and run details.

### Security posture

**The server executes user-provided Python locally. It is not sandboxed.**
Timeout + output-cap + process-tree kill is a **robustness** boundary, not
a security sandbox. The child is also launched with its stdin closed
(`DEVNULL`) so a script that reads `input()`/`sys.stdin` gets an immediate
EOF instead of consuming the server's JSON-RPC stream when running over
stdio. There is no AST filtering, no network blocking, no import allowlist.
This tool is local-only; a cloud/multi-tenant deployment would require a
real sandbox. The **server wrapper** makes no network calls,
but the executed child process is arbitrary code.

### Example scripts

`examples/cpsat_python/` holds reference scripts with the canonical emit
snippet:

- **`examples/cpsat_python/assignment.py`** — 4 workers × 4 tasks, maximize total score.
- **`examples/cpsat_python/scheduling.py`** — 3 tasks on a single machine, minimize makespan.

Both can be run standalone (`python examples/cpsat_python/assignment.py`) and
are used as integration-test anchors for `run_cpsat_python`.

### MiniZinc vs. CP-SAT Python

| | MiniZinc path | CP-SAT Python path |
|---|---|---|
| Input | Declarative model (`.mzn`) | Executable Python (`ortools`) |
| Execution | Managed MiniZinc runtime | Local child process |
| Install | `install-runtime` needed | Zero-install (ortools bundled) |
| Sandboxing | Runtime reads model, no exec | **Not sandboxed** |
| LLM fluency | High (MiniZinc is LLM-friendly) | High (Python is LLM-friendly) |

Use MiniZinc for declarative, verifiable models where the managed runtime
provides the execution boundary. Use the CP-SAT Python path when the problem
is naturally imperative, needs custom Python data structures, or you prefer
direct OR-Tools APIs.

## MCP prompts

The stdio server exposes two MCP prompts for client-side LLMs:

- **`solve_constraint_problem(problem: str)`** — a guided template for the
  MCP client's LLM. Given a natural-language constraint or optimization
  problem, the prompt instructs the client's model to:

  1. Identify decision variables, domains, constraints, and any objective.
  2. Ask the user a few concise clarifying questions if the problem is
     underspecified, rather than silently inventing values.
  3. Draft a complete MiniZinc model — including declarations,
     constraints, exactly one `solve` statement, and an `output` block —
     preferring the `cp-sat` solver by default.
  4. Validate the drafted model with `check_minizinc_model` before
     solving, when that tool is available: solve only after the check
     returns `"ok"`; on `"error"`, repair the model from `stderr` and
     re-check; on `"timeout"`, ask the user how to proceed (simplify the
     model, raise `timeout_ms`, or solve anyway) rather than auto-solving.
  5. Call the `solve_minizinc_model` tool if it is available, or
     otherwise walk the user through the openconstraint-mcp CLI —
     `check-runtime` to locate the managed `minizinc` binary (with
     `install-runtime` or `configure-runtime` first if it is missing) —
     and have them invoke that exact managed binary on the drafted
     model. The prompt explicitly forbids recommending a bare
     PATH-based `minizinc` invocation.
  6. Revise the model if MiniZinc reports an error, and present the final
     result to the user as a short, structured summary that leads with the
     result: a plain-language `status`, the solution quoted verbatim from
     `stdout` (only when the status carries one), a compact table rather than
     a prose-only list when the data is item-like (one row per item for small
     item sets, with relevant attributes and the selected/count value), and
     the complete model-visible `Statistics:` section whenever the
     `statistics` map is non-empty. Do not condense that section to selected
     fields such as `solveTime` and `objectiveBound`. Each section heading
     appears at most once, and the explanation stays focused on verifying the
     result rather than adding speculative algorithm commentary by default.
  7. Optionally — only when the user asks to save the result — persist it
     with `save_verified_minizinc_model`, passing the final model/data/checker
     text and the user's explicit absolute target directory. The client asks
     the user for that path (or uses its own file picker); the server opens no
     file dialog and re-verifies the artifacts before writing anything.

  When the user already has the model on disk as `.mzn`/`.dzn` files, the
  prompt skips drafting and routes the same validate → solve → present loop
  through the path-based `check_minizinc_files` and `solve_minizinc_files`
  tools (passing `model_path`/`data_path`), which return the same
  `CheckResult`/`SolveResult` shapes.

  The openconstraint-mcp server itself does **not** call an LLM and does
  not embed any agent framework. The prompt only structures how the
  *client's* LLM should propose a MiniZinc model; the model is then
  verified by the local managed MiniZinc runtime via
  `solve_minizinc_model`. `LLM proposes, local MiniZinc verifies.`

- **`solve_cpsat_python(problem: str)`** — a guided template for the MCP
  client's LLM to write OR-Tools CP-SAT Python and run it via
  `run_cpsat_python`. The prompt instructs the client's model to:

  1. Identify decision variables, domains, constraints, and the objective.
  2. Ask concise clarifying questions if the problem is underspecified.
  3. Write a complete, runnable OR-Tools CP-SAT Python script that emits
     the required JSON object (`{"status", "objective", "solution"}`) as its
     last stdout line, using `status_map` to translate `cp_model.OPTIMAL`
     etc. to vocabulary strings. For reproducible saved artifacts, set a
     fixed `solver.parameters.random_seed` and prefer a single search
     worker. **Safety instruction:** generate only CP-SAT modeling code —
     no network access, no file writes or deletes, no subprocess spawning —
     unless the user explicitly asked. The server executes this code locally
     and does not sandbox it.
  4. Call `run_cpsat_python` with the script as `source`.
  5. Present the `CpsatPythonResult`: distinguish `optimal` (proven best)
     from `feasible` (valid but not proven optimal); point at `stderr` on
     `error`; explain `timeout` clearly.
  6. Optionally — only when the user asks — call `save_verified_cpsat_python`
     with the script and an explicit absolute `target_dir`. The client asks
     the user for that path; the server opens no file dialog and re-runs the
     script to verify before writing anything.

  The server makes no LLM call. The prompt structures how the *client's*
  LLM should write the script; the script is then executed locally by
  `run_cpsat_python`. `LLM writes, server executes locally.`

## Example models

The `examples/` directory holds small, self-contained MiniZinc models you can
point the path-based file tools at (or run by hand through the managed
runtime). Each is a `model.mzn` — usually with a matching `data.dzn`, and one
also ships a `model.mzc.mzn` solution checker:

- **`examples/knapsack`** — bounded knapsack: choose how many of each item type
  to pack to maximize total value without exceeding the weight `capacity`
  (`solve maximize`).
- **`examples/balanced_assignment`** — assign jobs to workers to minimize the
  most-loaded worker's total duration, i.e. balance the load (`solve minimize`).
- **`examples/social_golfers`** — the Social Golfer Problem: schedule `n_groups`
  groups of `group_size` golfers over `n_weeks` weeks so no pair ever shares a
  group twice (`solve satisfy`). The shipped data is the 6-3-8 instance — 18
  golfers in 6 groups of 3 over 8 weeks, the most weeks a 6-3 schedule can reach
  before some pair must repeat. The CP-SAT examples under `cpsat/` and
  `cpsat_24/` are specialized Python constructions for the 7-3-10 and 8-3-11
  boundary instances.
- **`examples/australia_map_coloring`** — colour Australia's seven
  states/territories with three colours so no two bordering regions share one
  (`solve satisfy`). Its data (`nc = 3`) is inline, so there is no `data.dzn`;
  instead it ships a `model.mzc.mzn` solution checker, so it doubles as a
  demonstration of the checker feature (see below).

For instance, to solve the knapsack example end to end:

```jsonc
// solve_minizinc_files
{
  "model_path": "examples/knapsack/model.mzn",
  "data_path": "examples/knapsack/data.dzn"
}
```

(prefer absolute paths in real MCP calls — see *Path-based file tools* above).

To run the Australia example *with* its solution checker, point `checker_path`
at the shipped `.mzc.mzn`:

```jsonc
// solve_minizinc_files
{
  "model_path": "examples/australia_map_coloring/model.mzn",
  "checker_path": "examples/australia_map_coloring/model.mzc.mzn"
}
```

The resulting `SolveResult.checker` report then carries the checker's
per-solution verdict (here, `CORRECT`).

The social-golfers model is parameterized through its `data.dzn`, which enables
two workflows beyond a single solve:

- **Longest schedule.** "As many weeks as possible" is the same model re-solved
  with `n_weeks` raised until it turns unsatisfiable. For the shipped instance,
  `n_weeks = 8` solves but `n_weeks = 9` is `unsatisfiable` (only `C(18,2) = 153`
  pairs exist, and 9 weeks would need 162 distinct ones), so 8 is the maximum.
- **Multiple schedules.** To enumerate several distinct schedules, lower
  `n_weeks` (e.g. to 5) and request more than one solution with a solver that
  supports it — `num_solutions` works with `org.gecode.gecode` or
  `org.chuffed.chuffed`, not the default `cp-sat`.

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

### Installing the managed runtime

`openconstraint-mcp install-runtime` is the supported way to put a managed
MiniZinc bundle on disk. The first invocation:

1. Resolves the install location. Precedence is `--runtime-dir` > the env var >
   the persisted install config > the platformdirs default
   (`<platformdirs user_data_dir>/minizinc`).
2. Streams the pinned MiniZinc bundle for your platform from the official
   MiniZinc GitHub release — the Linux x86_64 `.tgz`; the macOS `.dmg` on
   Apple Silicon (mounted read-only via `hdiutil` and reshaped into the same
   `bin`/`lib`/`share` layout); or, on Windows x86_64, the NSIS
   `setup-win64.exe` run silently (`setup.exe /S /D=<runtime>`) into the managed
   runtime directory — verifies its SHA256, installs it safely, and
   smoke-checks the resulting `bin/minizinc` (`bin\minizinc.exe` on Windows). On
   macOS the bundled Gecode is the
   Qt-linked build, so the installer vendors its Qt frameworks into
   `<runtime>/Frameworks` and relinks the solver to load them headlessly (no
   GUI is ever launched). That relink step uses the Xcode Command Line Tools, so
   run `xcode-select --install` first if `install-runtime` reports
   `install_name_tool` is missing.
3. Writes a small JSON config (`<platformdirs user_config_dir>/install.json`,
   typically `~/.config/openconstraint-mcp/install.json` on Linux) recording the
   chosen path.

On Windows, the NSIS installer requests administrator rights, so the first
`install-runtime` shows a one-time Windows UAC elevation prompt — confirm it to
let the silent install finish.

Once that config is written, subsequent `check-runtime` and `list-solvers` calls
find the runtime automatically — no env-var fiddling. To reset, delete the
config file, or set `OPENCONSTRAINT_MCP_RUNTIME_DIR` (the env var always wins).
If the config file is present but corrupt (e.g. hand-edited into invalid JSON),
`check-runtime` and `list-solvers` print a warning to stderr and fall back to the
default location rather than failing silently.

If you pass `--runtime-dir <path>` again on a later install, the new path
replaces the old one in the config. The previous runtime directory is not
touched and can be deleted manually.

A successful install also writes a `.openconstraint-runtime.json` marker into
the runtime directory itself. Future `install-runtime` invocations check that
marker before overwriting: an unmanaged non-empty directory is refused
regardless of `--yes`, which makes `--runtime-dir $HOME --yes` (or similar)
safe — your home directory cannot be wiped by a fat-finger.

### Startup diagnostic

On startup the MCP server prints a short three-line diagnostic to **stderr**:
the server version, the resolved runtime directory, and whether the managed
runtime is installed (with an `install-runtime` hint when it is absent). This
banner is **stderr-only by design** — over the stdio transport, `stdout` is the
JSON-RPC protocol channel, so the diagnostic never touches it. The banner only
*reads* the already-resolved runtime status; it never downloads or installs
anything. MCP clients that hide server stderr simply will not show it.

The server also advertises its project `Homepage` to MCP clients via the
`website_url` field, sourced from the package metadata (single source of truth:
`[project.urls]` in `pyproject.toml`).

## v0 limitations

This is an early release; the focus is "easy install, reliable solving, clear
errors" rather than feature breadth. In particular:

- **The automated installer covers Linux x86_64, macOS arm64 (Apple Silicon),
  and Windows x86_64.** Windows ARM, Linux ARM, and macOS x86_64 (Intel) bundles
  are tracked separately. On those platforms, `install-runtime` exits 1 with a
  clear message — use `configure-runtime --runtime-dir <path>` or point
  `OPENCONSTRAINT_MCP_RUNTIME_DIR` at an existing MiniZinc install (a directory
  containing `bin/minizinc`) in the meantime.
- **No telemetry, ever**, unless and until you explicitly opt in to a clearly
  labelled future feature.
- **The only code path that touches the network is the `install-runtime` CLI
  command.** The package does not phone home; `httpx` is only imported when
  `install-runtime` runs (enforced by a regression test).

## Licensing & upstream sources

`openconstraint-mcp` is licensed under the Apache License 2.0; see `LICENSE`.
The MiniZinc runtime it wraps is
**fetched** from the official MiniZincIDE GitHub release at install time — the
Linux x86_64 `.tgz`, the macOS `.dmg`, or the Windows x86_64 NSIS
`setup-win64.exe`, depending on your platform — this repository does **not**
redistribute MiniZinc or its bundled solvers.

The upstream bundle includes:

- MiniZinc itself (the constraint modelling language and its compiler).
- Gecode, Chuffed, OR-Tools CP-SAT, COIN-BC, and other solvers shipped with the
  MiniZincIDE bundle. Their licenses are surfaced upstream — see
  [minizinc.org](https://www.minizinc.org/) for the license index, or the
  per-solver entries on the
  [MiniZincIDE release page](https://github.com/MiniZinc/MiniZincIDE/releases).

After `install-runtime`, each bundled component's license file lives inside the
installed runtime tree (typically under `<runtime_dir>/share/minizinc/...` and
adjacent directories) and is left untouched by the installer. For a single
authoritative document, the MiniZincIDE release page is the recommended source.
