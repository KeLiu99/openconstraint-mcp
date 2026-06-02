# openconstraint-mcp

[![CI](https://github.com/KeLiu99/openconstraint-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/KeLiu99/openconstraint-mcp/actions/workflows/ci.yml)

A local-first [Model Context Protocol](https://modelcontextprotocol.io) server for
constraint programming and optimization. `openconstraint-mcp` gives an MCP client a
deterministic way to compile-check and solve [MiniZinc](https://www.minizinc.org/)
models on a **managed** solver runtime — controlled by this project, not your system
install — exposing open-source solvers (OR-Tools CP-SAT by default,
Chuffed as an optional verifier) over MCP stdio.

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
   - `openconstraint-mcp install-runtime` to fetch and install the managed bundle (Linux x86_64).
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
  MiniZinc bundle (Linux x86_64 only in v0). Streams the pinned upstream archive
  from the MiniZinc GitHub release, verifies its SHA256, extracts it into the
  chosen target, smoke-checks the resulting `bin/minizinc`, and remembers the
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

The stdio server exposes two introspection tools, a model-check tool, an
execution tool, and an unsat-core diagnostic tool — each of the latter three
in an **inline-source** form (below) and a **path-based file** sibling
([Path-based file tools](#path-based-file-tools)):

- **`check_runtime`** — returns a `RuntimeStatus` with fields
  `installed: bool`, `runtime_dir: str`, and `minizinc_binary: str | None`.
- **`list_available_solvers`** — returns a `SolverList` of `SolverInfo` entries
  (`id`, `name`, `version`, `tags`). Raises a runtime-missing error if the
  managed MiniZinc binary is not present.
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
  - `solver: str = "cp-sat"` — passed through verbatim to MiniZinc's
    `--solver` flag.
  - `timeout_ms: int = 30000` — solving budget in milliseconds. Must be
    strictly positive. `0` is **not** "no timeout" — it is a validation
    error. Pass a real budget, or omit the argument to get the default.

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
  - `stdout: str` — the runtime's raw stdout (solution lines and separator
    markers from the model's `output` block). Solve runs pass MiniZinc's
    `--statistics`, so `stdout` also carries `%`-comment `%%%mzn-stat:` lines
    that a baseline run would not emit.
  - `stderr: str` — the runtime's raw stderr (compile, type, and solver
    errors land here).
  - `elapsed_ms: int` — wall-clock duration of the subprocess call.
  - `statistics: dict[str, str]` — best-effort raw `%%%mzn-stat:` key/value
    pairs parsed from `stdout` (values kept verbatim, not coerced). May be
    `{}` when none were emitted; the key set is solver- and version-defined,
    **not** a stable contract; raw `stdout` stays authoritative. It is a
    **non-authenticated** view: `stdout` is one stream, so a model's `output`
    block can print `%%%mzn-stat:`-shaped lines that land in this dict — do
    not treat it as tamper-proof.

  The MCP response also includes model-visible text content with status,
  solver metadata, raw stdout/stderr, and a `Statistics:` section whenever
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

### Path-based file tools

The three tools above take the model (and optional data) as **inline source
text**, which the server writes to a private temp file. That is ideal for the
small/medium models a client LLM drafts, but it forces the agent to read an
entire `.mzn`/`.dzn` from disk and thread the whole contents through MCP
arguments. For large local models the server also exposes **path-based**
siblings that read the model/data from local file paths instead:

- **`check_minizinc_files`** — path-based sibling of `check_minizinc_model`.
- **`solve_minizinc_files`** — path-based sibling of `solve_minizinc_model`.
- **`find_unsat_core_files`** — path-based sibling of `find_unsat_core`.

Each returns the **same** result shape as its inline counterpart
(`CheckResult` / `SolveResult` / `UnsatCoreResult`). The inline tools are
unchanged and remain the right choice for ephemeral, isolated text workflows.

**Arguments** (all three):

- `model_path: str` — path to a local `.mzn` file on the machine running the
  server. Required; must exist and be a regular file.
- `data_path: str | None = None` — path to a local `.dzn` file, or `null`. An
  empty data file is allowed (a valid "no parameters" input).
- `solver: str = "cp-sat"` — `solve`/`check` only (not `find_unsat_core_files`,
  which always uses findMUS).
- `timeout_ms: int = 30000` — same semantics as the inline tools; must be
  strictly positive.

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

## MCP prompts

The stdio server also exposes one MCP prompt for client-side LLMs to use:

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
2. Streams the pinned MiniZinc bundle from the official MiniZinc GitHub release,
   verifies its SHA256, extracts it safely, and smoke-checks the resulting
   `bin/minizinc`.
3. Writes a small JSON config (`<platformdirs user_config_dir>/install.json`,
   typically `~/.config/openconstraint-mcp/install.json` on Linux) recording the
   chosen path.

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

- **The automated installer is Linux x86_64 only.** macOS, Windows, and Linux
  ARM bundles are tracked separately. On those platforms, `install-runtime`
  exits 1 with a clear message — point `OPENCONSTRAINT_MCP_RUNTIME_DIR` at an
  existing MiniZinc install (a directory containing `bin/minizinc`) in the
  meantime.
- **No telemetry, ever**, unless and until you explicitly opt in to a clearly
  labelled future feature.
- **The only code path that touches the network is the `install-runtime` CLI
  command.** The package does not phone home; `httpx` is only imported when
  `install-runtime` runs (enforced by a regression test).

## Licensing & upstream sources

`openconstraint-mcp` is licensed under the Apache License 2.0; see `LICENSE`.
The MiniZinc runtime it wraps is
**fetched** from the official MiniZincIDE GitHub release at install time — this
repository does **not** redistribute MiniZinc or its bundled solvers.

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
