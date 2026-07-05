"""MCP tool and prompt description strings.

These are protocol-contract texts — what MCP clients see as tool/prompt
documentation.  Keeping them here lets server.py focus on wiring.
"""

# Shared description fragments spliced into the constants below, so a wording fix
# lands in one place instead of drifting across copies. This is the same pattern
# as _FILE_TOOL_SHARED_DESCRIPTION, applied to the cross-cutting guarantees the
# solve/job/portfolio tools repeat. Helpers cover near-duplicates that vary only
# by a single token (the job getter name, the terminal-states list).

_LOCAL_ONLY_GUARANTEE = (
    "Runs locally through the managed runtime: no network, no LLM, no telemetry."
)

# CP-SAT tools execute arbitrary Python under the server's own interpreter, not
# the managed runtime, so the wrapper's offline guarantee cannot extend to the
# child — keep that posture honest rather than reusing _LOCAL_ONLY_GUARANTEE.
_CPSAT_CHILD_POSTURE = (
    "Network posture: the server wrapper makes no network, LLM, or telemetry "
    "calls, but the child is arbitrary, unsandboxed Python run under the "
    "server's interpreter — it can open sockets, import `requests`, or shell "
    "out. 'Offline' describes the wrapper here, not the executed script."
)

_UNKNOWN_JOB_ID_ERROR = "An unknown `job_id` is an MCP error."

_NO_ARGS_LIST_TOOL = "Takes no arguments; never downloads or runs anything."

_REGISTRY_NOTE = (
    "The registry is in-process and ephemeral: jobs don't survive a server "
    "restart, and finished jobs are retained only up to a cap (oldest evicted)."
)

_SOLVE_CONTROLS_LIST = (
    "`free_search`, `parallel`, `random_seed`, `all_solutions`, and the "
    "solver-gated, satisfaction-only `num_solutions`"
)


def _returns_immediately_note(get_tool: str) -> str:
    """Background-submit tail: returns at once, watch `state` via `get_tool`."""
    return (
        "Returns at once, so it emits no progress/log status milestones; watch "
        f"`state` via `{get_tool}` instead. "
    )


def _cancellation_idempotent_note(terminal_states: str) -> str:
    """Shared cancel-tool idempotency sentence; `terminal_states` varies per tool."""
    return (
        "Cancellation is best-effort and idempotent: cancelling an "
        f"already-terminal job ({terminal_states}) is a no-op returning the "
        "current status unchanged. "
    )


MCP_SERVER_INSTRUCTIONS = (
    "Use this MCP server for local constraint programming and optimization: "
    "MiniZinc and CP-SAT models for scheduling, rostering, knapsack, "
    "allocation, assignment, routing, bin-packing, SAT/UNSAT analysis, model "
    "validation, and solver statistics. For natural-language problems, prefer "
    "the solve_constraint_problem prompt when the client supports MCP prompts; "
    "otherwise draft MiniZinc in the client LLM, check it with "
    "check_minizinc_model, then solve with solve_minizinc_model. To learn which "
    "parameters a model needs as data (plus its output variables and solve "
    "method) before building a `.dzn`, use inspect_minizinc_model. When the "
    "model and data already exist as local files, pass their paths to "
    "check_minizinc_files / solve_minizinc_files instead of pasting contents — "
    "the path-based tools run from the model's directory, so a relative "
    "`include` resolves. Validate solutions by passing a checker to the solve "
    "tools (`checker` inline, `checker_path` for file solves). When the user "
    "asks to save a successful checked/solved inline model, the client can "
    "persist it with save_verified_minizinc_model and an absolute `target_dir`; "
    "the server re-verifies before writing and "
    "never opens a file dialog. Either way, lead with the result: a "
    "plain-language status, the stdout solution stated in the terms of the "
    "user's problem (not the raw JSON SolveResult), a compact item table when "
    "the problem supplies item-like data, and the complete model-visible "
    "`Statistics:` section when present — do not condense it to selected fields. "
    "Use `num_solutions` only with `org.gecode.gecode` or `org.chuffed.chuffed`, "
    "not the default `cp-sat`; for multiple optimal solutions, solve the "
    "optimization first, then re-solve as satisfaction with the objective fixed "
    "to the proven optimum. The check/inspect/solve/unsat-core tools emit status "
    "feedback while MiniZinc runs: MCP progress notifications when the request "
    "carries `_meta.progressToken`, plus info-level log notifications — stage "
    "markers, not a completion percentage, so never render a percent bar. "
    "MiniZinc tools use the managed local MiniZinc runtime; never a remote "
    "solver or a bare PATH minizinc. "
    "For richer expressiveness or an independent verification pass, use MiniZinc. "
    "A second, parallel path executes OR-Tools CP-SAT Python locally: use the "
    "`solve_cpsat_python` prompt to guide the client LLM to write a conforming "
    "script, then run it with `run_cpsat_python` (bounded child process, timeout "
    "+ 1 MB output cap + tree-kill, returns `CpsatPythonResult`), and persist "
    "the solution with `save_verified_cpsat_python` (save gate: reported "
    "status + optional expectation threshold and/or checker script). The server executes "
    "user-provided Python locally — it is NOT sandboxed; this is a local-only "
    "tool. The server wrapper makes no network calls, but the executed child is "
    "arbitrary code. Use MiniZinc for declarative/verifiable models; use the "
    "CP-SAT Python path for imperative logic, custom data structures, or when "
    "the managed MiniZinc runtime is not installed."
)

CHECK_RUNTIME_DESCRIPTION = "Report whether the managed MiniZinc runtime is installed."

LIST_AVAILABLE_SOLVERS_DESCRIPTION = (
    "List solvers in the managed MiniZinc runtime. Returns a SolverList of "
    "SolverInfo entries — each with `id`, `name`, `version`, `tags`, and a "
    "`capabilities` object of deterministic facts read from the runtime's own "
    "`--solvers-json` config, for client-side solver routing. "
    "`capabilities.supports_all_solutions` (`-a`), `supports_free_search` "
    "(`-f`), `supports_parallel` (`-p`), and `supports_random_seed` (`-r`) "
    "report membership in the solver's declared `stdFlags`. "
    "`supports_num_solutions` (`-n`) is NOT a raw stdFlags read but the "
    "conservative gate matching the `num_solutions` control — True only for "
    "`org.gecode.gecode` and `org.chuffed.chuffed`, not the default `cp-sat`. "
    "The four `-a/-f/-p/-r` facts are ENFORCED: a requested "
    "`all_solutions`/`free_search`/`parallel`/`random_seed` is rejected before "
    "solving when the solver's `stdFlags` omit the flag. Enforcement matches by "
    "exact canonical solver `id` (like the `num_solutions` gate), so select "
    "non-default solvers by canonical id to get upfront rejection — a short "
    "alias (e.g. `gecode`) or unknown solver does not resolve and passes through "
    "to MiniZinc unchanged. The advisory `std_flags` list reports the flags the "
    "solver declares; it is NOT a passthrough surface — a client cannot send "
    "those flags back into `solve_minizinc_model` / `solve_minizinc_files`. Keep "
    "two cases distinct: (a) `std_flags` may list flags with no named control at "
    "all (e.g. `-i`, `-s`, `-t`, `-v`) — purely informational; (b) the allowlist "
    "divergence — `org.gecode.gist` lists `-n` (which maps to `num_solutions`) "
    "yet `supports_num_solutions` is False, because the gate excludes gist. The "
    "text content presents a complete `id`/`name`/`version` inventory table of "
    "every solver, with a final-answer requirement to copy it without omitting "
    "rows, converting to bullets/prose, summarizing, or grouping, plus a "
    "user-visible note that detailed capabilities can be requested. The "
    "SolverList carries that `capability_note`. The full `capabilities` metadata "
    "(`supports_*` booleans and `std_flags`) lives in the structured result and "
    "is not printed by default — request it explicitly to surface it."
)

SOLVE_MINIZINC_MODEL_DESCRIPTION = (
    "Solve a complete MiniZinc model through the managed local runtime. `model` "
    "must be full source: declarations, constraints, exactly one `solve` "
    "statement, and an `output` block. Optional `data` is `.dzn` text run as a "
    "data file beside the model (omit when none is needed). Returns a "
    "SolveResult: `status`, `solver`, `return_code` (null on a subprocess "
    "timeout), `timed_out`, `elapsed_ms`, `stdout` (human-readable solution "
    "text, rebuilt from the solve stream's output sections), `stderr` "
    "(diagnostics — model/solver errors and warnings, so you can revise and "
    "retry), `solution` (best/last solution as a variable -> value map, model "
    "variables only), `solutions` (every solution in order; its last entry is "
    "`solution`), `objective` (best objective value, null for pure "
    "satisfaction), `statistics` (best-effort map, may be empty, solver-defined "
    "keys), and `checker` (null unless a checker was supplied). Structured "
    "values come from the runtime's machine-readable solve stream, not scraped "
    "text. The text content includes a `Statistics:` section whenever that map "
    "is non-empty; copy the entire section verbatim into the answer rather than "
    "summarizing selected fields. Optional solver/search controls (default to "
    "current behavior, solve-only): `free_search` (`-f`: solver's own search "
    "instead of the model's annotations — solver-dependent, not 'no search'); "
    "`parallel` (int >= 1 -> `-p`: search threads); `random_seed` (int -> `-r`); "
    "`all_solutions` (`-a`: enumerate all solutions, or the optimization "
    "improving-sequence, into `solutions`); `num_solutions` (int >= 1 -> `-n`: "
    "cap solutions for a SATISFACTION problem; SOLVER-GATED to `org.gecode.gecode` "
    "or `org.chuffed.chuffed`, NOT the default `cp-sat` — any other solver "
    "returns an actionable error; for optimization use `all_solutions`). The "
    "`-a/-f/-p/-r` controls are capability-gated: if the solver's runtime-local "
    "`stdFlags` omit the flag, the request is rejected before solving with an "
    "error naming the solver, control, and flag (canonical-id match — a short "
    "alias or unknown solver passes through); call `list_available_solvers` for "
    "each solver's `supports_*` facts. Optional `checker` is inline MiniZinc "
    "checker source written beside the model and passed via `--solution-checker`; "
    "it may include the co-located `model.mzn` but cannot resolve arbitrary "
    "project-relative includes — use `solve_minizinc_files` with `checker_path` "
    "for multi-file checkers. When supplied, `checker` is a nested CheckerReport "
    "with `status` (`completed`, `violation`, `no_solution`, `error`, "
    "`timeout`), `checks` (one verdict per solution, index-aligned with "
    "`solutions`), and `transcript` (the AUTHORITATIVE raw `--json-stream` "
    "transcript of solve + checker objects). IMPORTANT: author "
    "`CORRECT`/`INCORRECT` text is surfaced verbatim in `checks[].output` and is "
    "NOT interpreted by the server — only a nested UNSATISFIABLE sets "
    "`violation`. The checker validates each solution but never proves "
    "optimality — `status` remains the proof of completeness/optimality. "
    "`structuredContent` carries the complete SolveResult. For a HARD "
    "instance — status is unknown/timeout, or the best formulation/solver/seed "
    "choice is unclear — consider `submit_portfolio_job` to race multiple "
    "formulations, solvers, and seeds instead of one run; for an especially "
    "hard instance, also consider the OR-Tools CP-SAT Python path "
    "(`run_cpsat_python`) for the same problem."
)

CHECK_MINIZINC_MODEL_DESCRIPTION = (
    "Compile-check a complete MiniZinc model through the managed local runtime "
    "WITHOUT solving it — flattening it for the chosen solver to catch syntax, "
    "type, missing-include, invalid-domain, and unsupported-construct errors so "
    "you can repair it before `solve_minizinc_model`. Optional `data` is `.dzn` "
    "text; a parameterized model needs the same `data` you'll pass to the solve "
    "in order to flatten (omit when none is needed). Returns a CheckResult: "
    "`status` (`ok`/`error`/`timeout`), `solver`, raw `stdout`/`stderr`, "
    "`elapsed_ms`. `ok` means it compiles, not that it is satisfiable."
)

INSPECT_MINIZINC_MODEL_DESCRIPTION = (
    "Inspect a MiniZinc model's INTERFACE through the managed local runtime "
    "WITHOUT solving it — report which parameters it needs as data, which "
    "variables it outputs, their types (array `dim`, set-ness), and the solve "
    "`method` (`sat`/`min`/`max`), so you can build correct `.dzn` data before "
    "spending a solve. Optional `data` is `.dzn` text run beside the model (omit "
    "when none is needed). Returns a ModelInspectionResult: `status` "
    "(`ok`/`error`/`timeout`), `solver`, raw `stdout`/`stderr`, `elapsed_ms`, "
    "and — only when `ok` — a structured `interface` with `method`, "
    "`required_parameters`, `output_variables`, `has_output_item`, `globals`, "
    "`included_files`. `required_parameters` is the set STILL needing a value "
    "given any `data` you passed: with no data it is the full required set; "
    "matching data shrinks it, and an empty `required_parameters` means the data "
    'is complete. IMPORTANT: `status="ok"` means only that the interface was '
    "extracted — it is NOT a data-completeness signal; only "
    "`required_parameters == {}` is. `output_variables` is advisory (output "
    "variables, not necessarily every decision variable). Enum-typed entries "
    "appear as `int`; enum names are not surfaced in v1."
)

FIND_UNSAT_CORE_DESCRIPTION = (
    "Diagnose an unsatisfiable MiniZinc model by computing a minimal "
    "unsatisfiable subset (MUS) of its constraints via the managed runtime's "
    "findMUS tool. Use it when solve_minizinc_model returns 'unsatisfiable'. "
    "Optional `data` is `.dzn` text; pass the SAME `data` you solved with (omit "
    "when none is needed). Returns an UnsatCoreResult: `status` "
    "(`mus_found`/`no_core`/`error`/`timeout`), `core`, `message`, raw "
    "`stdout`/`stderr`, `elapsed_ms`. `core` is a best-effort structured list "
    "(source span + text) resolved from the MODEL FILE only — a decision "
    "variable assigned in `data` acts as a constraint, so a MUS member can "
    "originate in the data file and appear in authoritative `stdout` but not in "
    "`core`. The subset is MINIMAL but not necessarily the globally smallest, "
    "and a model may have several."
)

SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION = (
    "Save a successful inline MiniZinc workflow to a LOCAL project directory "
    "AFTER re-verifying it through the managed local runtime. The server trusts "
    "no prior claim of success: it re-runs the compile check and solve on "
    "`model` (with optional `data` and inline `checker`), and writes only when "
    "the check is `ok` and the solve is `satisfied`/`optimal` with a clean exit "
    "and no timeout — and, if a checker is supplied, its nested report is "
    "`completed` (ran without machine-readable violation; NOT a proof of "
    "optimality). `target_dir` must be an EXPLICIT ABSOLUTE local directory "
    "whose parent exists; the server never opens an OS file dialog — the client "
    "supplies the path. Optional `portfolio_result` (a PortfolioSolveResult from "
    "a MiniZinc solver-portfolio race) attaches that race's attempt table as "
    "PROVENANCE ONLY — never verification evidence; the save still re-runs "
    "check/solve/checker on `model`/`data`/`solver`/`random_seed` fresh and "
    "gates on that alone. Eagerly rejected (MCP error, before any check/solve) "
    "when: `portfolio_result.status != 'winner'`; the winning attempt's "
    "`solver` or `seed` does not match this save's `solver`/`random_seed` (an "
    "unseeded winner matches an unseeded save); the winning formulation's/"
    "data's hash does not match `model`/`data`; or the race's shared "
    "`solve_controls` (`free_search`/`parallel`/`all_solutions`/"
    "`num_solutions`) do not match this save's — the save must replay the "
    "winning attempt's search configuration (`timeout_ms`, a budget, is not "
    "gated). A `checker_sha256` mismatch is "
    "NOT rejected — it only affects what the persisted log records. When "
    "supplied and every gate passes, `experiment-log.json` is written (role "
    "`experiment_log` in `files`) recording every portfolio attempt (model "
    "index, solver, seed, timeout, state, result status, checker status, "
    "objective, timing), the race's shared solve controls, plus a compact "
    "summary under the manifest's "
    "`verification`. Fixed filenames: `model.mzn`; `data.dzn`, "
    "`checker.mzc.mzn`, and `problem.md` only when `data`, `checker`, and "
    "`problem` (the user's original natural-language text, saved only when "
    "passed) are supplied; `solve-result.json` (the verifying SolveResult); "
    "`experiment-log.json` only when `portfolio_result` is supplied and the "
    "save succeeds; and a `.openconstraint-model.json` manifest recording tool "
    "version, timestamp, solver, the solve controls used, a verification "
    "summary, and per-file sha256 hashes. Overwrite is MARKER-GATED: a new or "
    "empty path is written "
    "directly; a non-empty directory is replaced wholesale (staged sibling + "
    "atomic swap) only when it holds a prior save's manifest marker, "
    "`overwrite=true` is passed, and it contains no files the prior save did not "
    "write; anything else is refused with an actionable error and nothing is "
    "touched. Accepts the same `solver`, `timeout_ms`, and solver/search "
    "controls as `solve_minizinc_model` (`free_search`, `parallel`, "
    "`random_seed`, `all_solutions`, and the solver-gated `num_solutions`); an "
    "`-a/-f/-p/-r` control the selected solver does not declare is rejected "
    "before any check, solve, or write. Returns a SaveVerifiedModelResult: "
    "`status` (`saved`/`not_verified`), `message`, the resolved `target_dir`, "
    "`files` (role, bare filename, sha256 — only on `saved`), `check` (always "
    "present), and `solve` (null when the check gate already failed). A model "
    "that fails any verification gate returns `not_verified` with the gating "
    "results and writes NOTHING; argument/path problems are MCP errors. " + _LOCAL_ONLY_GUARANTEE
)

# Shared guidance injected into each path-based file-tool description.
_FILE_TOOL_SHARED_DESCRIPTION = (
    "Reads the model (and optional data) from local FILE PATHS on the server's "
    "machine and runs the managed runtime from the model's own directory, so a "
    "relative `include` resolves like a hand-run `minizinc`. `model_path` is a "
    "required `.mzn` path (must exist, regular file); `data_path` is an optional "
    "`.dzn` path. Paths resolve to absolute (prefer absolute); a missing/non-file, "
    "empty, or non-UTF-8 model is an MCP error before any run. It reads the "
    "model, optional data, and any `include`d files; it never writes files, "
    "makes network calls, uploads data, or uses a remote solver."
)

CHECK_MINIZINC_FILES_DESCRIPTION = (
    "Compile-check a MiniZinc model from local file paths WITHOUT solving "
    "it — the path-based sibling of `check_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same CheckResult shape (`status` "
    "`ok`/`error`/`timeout`, `solver`, `stdout`, `stderr`, `elapsed_ms`); "
    "`ok` means it compiles, not that it is satisfiable."
)

SOLVE_MINIZINC_FILES_DESCRIPTION = (
    "Solve a MiniZinc model from local file paths — the path-based sibling "
    "of `solve_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same SolveResult shape (`status`, `solver`, "
    "`return_code`, `timed_out`, `elapsed_ms`, `stdout`, `stderr`, `solution`, "
    "`solutions`, `objective`, `statistics`, `checker`) and the same "
    "model-visible `Statistics:` summary whenever the parsed map is non-empty; "
    "copy the entire section rather than summarizing selected fields. Accepts "
    "the same solver/search controls as `solve_minizinc_model` ("
    + _SOLVE_CONTROLS_LIST
    + "), with the same upfront capability rejection of an `-a/-f/-p/-r` control "
    "the selected solver does not declare. Optional `checker_path` points to a "
    "`.mzc`/`.mzc.mzn` checker file, resolved to absolute and validated before "
    "any run; it adds `--solution-checker <path>` to the same invocation, so "
    "search controls compose with checking. When supplied, `checker` is the same "
    "nested CheckerReport as `solve_minizinc_model` — `status`, index-aligned "
    "`checks`, and authoritative raw `transcript`."
)

FIND_UNSAT_CORE_FILES_DESCRIPTION = (
    "Diagnose an unsatisfiable MiniZinc model from local file paths by "
    "computing a minimal unsatisfiable subset (MUS) via the managed "
    "runtime's findMUS tool — the path-based sibling of `find_unsat_core`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same UnsatCoreResult shape (`status` "
    "`mus_found`/`no_core`/`error`/`timeout`, `core`, `message`, `stdout`, "
    "`stderr`, `elapsed_ms`). `core` resolves from the ENTRY MODEL FILE "
    "only, so a MUS member in an INCLUDED file appears in authoritative "
    "`stdout` but NOT in `core`. The subset is MINIMAL but not necessarily "
    "the globally smallest."
)

INSPECT_MINIZINC_FILES_DESCRIPTION = (
    "Inspect a MiniZinc model's INTERFACE from local file paths WITHOUT "
    "solving it — the path-based sibling of `inspect_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESCRIPTION
    + " Returns the same ModelInspectionResult shape (`status` "
    "`ok`/`error`/`timeout`, `solver`, `stdout`, `stderr`, `elapsed_ms`, and "
    "the structured `interface` only when `ok`). `required_parameters` lists "
    "the parameters still needing a value given any `data_path`; an empty "
    '`required_parameters` means the data is complete, but `status="ok"` '
    "alone does NOT — it means only that the interface was extracted. Enum "
    "names are not surfaced in v1."
)

SUBMIT_SOLVE_JOB_DESCRIPTION = (
    "Submit a MiniZinc solve as a BACKGROUND JOB and return immediately, so a "
    "hard solve cannot hit a synchronous MCP client timeout. Takes the same "
    "inline surface as `solve_minizinc_model` — `model` (full source), optional "
    "`data`/`checker`, `solver`, `timeout_ms`, and the solver/search controls "
    + _SOLVE_CONTROLS_LIST
    + ". Argument errors (empty model, non-positive timeout, bad "
    "`parallel`/`num_solutions`) and an `-a/-f/-p/-r` control the selected "
    "solver does not declare are reported synchronously as MCP errors at "
    "admission, before any job exists. Returns a SolveJobStatus with a "
    "server-generated opaque `job_id` and `state` `queued` or `running`; poll "
    "with `get_solve_job(job_id)` and stop with `cancel_solve_job(job_id)`. "
    "Admission is BOUNDED: at most a fixed number of jobs run at once, further "
    "submits sit `queued` up to a cap, and a submit beyond that is REJECTED with "
    "an MCP error (retry once a running job finishes). "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_solve_job")
    + _LOCAL_ONLY_GUARANTEE
)

GET_SOLVE_JOB_DESCRIPTION = (
    "Poll a background solve job by its `job_id` (from `submit_solve_job`). "
    "Returns a SolveJobStatus: `job_id`, `state`, `solver`, `timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full SolveResult), and an optional `message`. "
    "`state` is one of `queued`, `running`, `succeeded`, `failed`, `timeout`, "
    "`cancelled`. CONTRACT: `result` is present exactly when `state` is "
    "`succeeded` or `timeout`, absent for `queued`/`running`/`failed`/"
    "`cancelled` — so branch on `state`, not on `result`. `failed` means the job "
    "machinery itself raised (no SolveResult, see `message`); a SOLVER-level "
    '`error` verdict is a `succeeded` job whose `result.status == "error"`, NOT '
    "`failed`. A `timeout` job still carries its partial SolveResult. While "
    "`running`, only `state` + `elapsed_ms` advance; live mid-solve statistics "
    "are not provided. PACE polling against the job's own budget, not a fixed "
    "`sleep`: a `running` job has roughly `timeout_ms - elapsed_ms` left and is "
    "usually terminal shortly after, so wait a fraction of the remaining budget "
    "between polls rather than looping tightly. On a `succeeded` or `timeout` "
    "job, present `result` as the synchronous solve tools require: lead with the "
    "plain-language status and the solution in the user's terms, and include the "
    "COMPLETE model-visible `Statistics:` section whenever `result.statistics` "
    "is non-empty — do not omit, summarize, or condense it to selected fields. "
    + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_SOLVE_JOB_DESCRIPTION = (
    "Request cancellation of a background solve job by `job_id`. A job still "
    "`queued` is dropped before it starts; a `running` job has its managed "
    "MiniZinc process tree (the solver children too) terminated. "
    + _cancellation_idempotent_note("`succeeded`/`failed`/`timeout`/`cancelled`")
    + "Returns the SolveJobStatus; the job reaches "
    "`cancelled` (with `result is None`) once the worker observes the request — "
    "poll `get_solve_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_SOLVE_JOBS_DESCRIPTION = (
    "List the currently retained background solve jobs as SolveJobStatus "
    "entries (one per job), covering every state from `queued` to terminal. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)

SUBMIT_PORTFOLIO_JOB_DESCRIPTION = (
    "Submit a solver portfolio as a BACKGROUND JOB and return immediately, so a "
    "hard race cannot hit a synchronous MCP client timeout. This is the "
    "supported way to run a portfolio: race several MiniZinc formulations, "
    "solvers, and seeds against ONE instance through the managed local runtime "
    "and return the SINGLE winner — a LOCAL race over the background-solve "
    "machinery, no remote/distributed solving, upload, or telemetry. Use it for "
    "a hard instance where you don't know which formulation or solver wins; an "
    "ordinary single-solver `solve_minizinc_model` is still the right first "
    "attempt. Takes the same inline surface as `solve_minizinc_model` — optional "
    "shared `data`/`checker` and the non-seed controls `free_search`, "
    "`parallel`, `all_solutions`, and the solver-gated, satisfaction-only "
    "`num_solutions` — applied identically to every attempt. Instead of one "
    "`model`/`solver`, pass a non-empty `models` list (alternative ENCODINGS of "
    "the same instance, sharing the one `data`/`checker` and controls — NOT a "
    "batch of different problems) and a non-empty `solvers` list. A high-value "
    "variant for a stalled CSP is a model with a restart annotation (e.g. "
    "`restart_luby`/`restart_geometric`) on its solve item: paired with multiple "
    "`seeds`, randomized restart escapes the heavy-tailed search that traps a "
    "single deterministic run. Restart-aware solvers (Gecode/Chuffed) honor "
    "these — include them in `solvers`; CP-SAT ignores them and runs its own "
    "restarts. Do NOT pass `random_seed`: use `seed_count` (shorthand) or "
    "`seeds` (exact values). With `seed_count == 1` and no `seeds`, each (model, "
    "solver) runs once UNSEEDED; with `seed_count > 1` each runs with seeds "
    "`1..seed_count` (every selected solver must support `-r`). With "
    "`seeds=[42, 123]` the portfolio uses exactly those seeds in order, with no "
    "extra unseeded attempt; `seeds` must be non-empty, contain no duplicates, "
    "cannot combine with `seed_count != 1`, and also requires every selected "
    "solver to support `-r`. There is no generic `solver_options`, `extra_args`, "
    "or raw MiniZinc flag passthrough. The plan is the full cross-product, model "
    "index varying fastest so the first attempts span distinct formulations — "
    "mind plan size, the cross-product grows fast. There is NO portfolio-side "
    "cap: every attempt is admitted; up to `max_running_jobs` (default 4) race "
    "simultaneously and the rest QUEUE, starting as running slots free, and a "
    "decisive running winner cancels the still-queued attempts before they "
    "start. Validation, capability enforcement, and admission happen "
    "SYNCHRONOUSLY here: an empty `models`/`solvers`, a bad control, an "
    "`-a/-f/-p/-r` flag the selected solver does not declare, or a plan that "
    "exceeds the job registry's running+queued capacity is reported at once as "
    "an MCP error, before any job exists. The attempts then run as ordinary jobs "
    "on the SAME bounded solve registry as `submit_solve_job` (so they count "
    "against its capacity and also appear in `list_solve_jobs`), and the winner "
    "is selected when you poll. Returns a PortfolioJobStatus with a "
    "server-generated opaque `job_id` and `state` `running`; poll with "
    "`get_portfolio_job(job_id)` — which advances the race and cancels the "
    "losers once a winner emerges — and stop the whole race early with "
    "`cancel_portfolio_job(job_id)`. "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_portfolio_job")
    + _LOCAL_ONLY_GUARANTEE
)

GET_PORTFOLIO_JOB_DESCRIPTION = (
    "Poll a background portfolio job by its `job_id` (from "
    "`submit_portfolio_job`). This also DRIVES the race: each poll selects a "
    "winner once one attempt reaches a decisive verdict and cancels the "
    "still-running losers, so poll until terminal rather than walking away. "
    "Returns a PortfolioJobStatus: `job_id`, `state`, `per_attempt_timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full PortfolioSolveResult), and an optional "
    "`message`. `state` is one of `running`, `succeeded`, `cancelled`. CONTRACT: "
    "`result` is present exactly when `state` is `succeeded`, absent for "
    "`running`/`cancelled` — so branch on `state`, not on `result`. A race that "
    "found no decisive winner is still `succeeded` (the orchestration completed) "
    "carrying a PortfolioSolveResult whose `status` is `no_winner`; a "
    "per-attempt failure is recorded in that result's attempts table, not as a "
    "failed job. `cancelled` means the client stopped the race. While "
    "`running`, only `state` + `elapsed_ms` advance; mid-race statistics are not "
    "provided. PACE polling against the race budget: a `running` race is usually "
    "terminal within roughly `per_attempt_timeout_ms`, so wait a fraction of it "
    "between polls rather than looping tightly. On a `succeeded` job, present "
    "`result` like a single `solve_minizinc_model`: lead with the winner's "
    "model/solver/seed/status, then the winning solve (solution + the COMPLETE "
    "`Statistics:` section) and the per-attempt table. The winning FORMULATION "
    "is `models[attempts[winner_index].model_index]`. " + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_PORTFOLIO_JOB_DESCRIPTION = (
    "Request cancellation of a background portfolio job by `job_id`, stopping the race "
    "AND every still-running attempt (each attempt's managed MiniZinc process tree is "
    "terminated). "
    + _cancellation_idempotent_note("`succeeded`/`cancelled`")
    + "Returns the PortfolioJobStatus; the job reaches "
    "`cancelled` (with `result is None`) once the race observes the request — poll "
    "`get_portfolio_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_PORTFOLIO_JOBS_DESCRIPTION = (
    "List the currently retained background portfolio jobs as PortfolioJobStatus "
    "entries (one per job), covering `running` and the terminal states. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)

RUN_CPSAT_PYTHON_DESCRIPTION = (
    "Execute LLM-generated OR-Tools CP-SAT Python source in a bounded child "
    "process and return a structured CpsatPythonResult. The script runs under "
    "the same Python interpreter as the server, with `ortools` and the stdlib "
    "available. It MUST emit a single JSON object to stdout as its last line: "
    '`{"status": "<status>", "objective": <float|null>, "solution": {<str: val>}}`. '
    "Valid `status` values: `optimal`, `feasible`, `infeasible`, `unknown`, `error`. "
    "Use the `solve_cpsat_python` prompt to generate conforming scripts. "
    "Returns a CpsatPythonResult: `status` (one of the above, or `timeout` if the "
    "process exceeded `timeout_ms`), `solution` (the parsed dict or null), "
    "`objective` (parsed float/int or null), `stdout`, `stderr`, `return_code` "
    "(null on timeout), `timed_out`, `truncated` (output exceeded 1 MB cap), "
    "`duration_ms`. A non-zero exit code, missing/unparseable JSON, or an "
    'off-vocabulary status string all yield `status="error"` with details in '
    "`stderr`/`stdout`. Output beyond 1 MB is truncated and the child killed. "
    "On `timeout`, `solution`/`objective` carry the last intermediate result "
    "block the script printed (the child runs unbuffered, so a best-so-far "
    "emitted from a CpSolverSolutionCallback survives), else null. For a HARD "
    "instance — status is unknown/timeout, or incumbent quality is unclear — "
    "consider the MiniZinc portfolio path (`submit_portfolio_job`) to race "
    "multiple formulations, solvers, and seeds for the same problem. " + _CPSAT_CHILD_POSTURE
)

RUN_CPSAT_PYTHON_FILE_DESCRIPTION = (
    "Execute an OR-Tools CP-SAT Python script from a LOCAL file path — the "
    "path-based sibling of `run_cpsat_python`. Pass `script_path` instead of "
    "pasting the whole source, so iterating on a local file does not mean "
    "re-copying it on every call. The script runs with its working directory set "
    "to the file's own directory, so a relative `open()` of a sibling data file "
    "or `import` of a helper module resolves (mirroring the MiniZinc file tools). "
    "`script_path` is resolved to absolute and validated before any run: a "
    "missing path, a non-file, an empty/whitespace-only script, or non-UTF-8 "
    "content is rejected with an actionable MCP error and nothing runs. Same "
    "execution contract, output cap, timeout, and tree-kill as `run_cpsat_python`: "
    "the script MUST emit a single JSON object to stdout as its last line "
    '(`{"status": "<status>", "objective": <float|null>, "solution": {<str: val>}}`), '
    "and the returned CpsatPythonResult has the identical shape (`status`, "
    "`solution`, `objective`, `stdout`, `stderr`, `return_code`, `timed_out`, "
    "`truncated`, `duration_ms`), including `timeout` partial recovery. " + _CPSAT_CHILD_POSTURE
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc)."
)

RUN_CPSAT_PYTHON_EXPERIMENT_DESCRIPTION = (
    "Run a list of EXPLICIT attempts — each a complete, independent OR-Tools "
    "CP-SAT Python `source` variant, optionally paired with a `seed` and/or a "
    "cooperative `config` — and return the best ACCEPTED result plus a compact "
    "attempt table. This generalizes a seed sweep into explicit attempts: the "
    "CLIENT proposes every attempt (source variants, config variants, or both); "
    "the server never generates, diffs, or merges attempts, and never sets "
    "OR-Tools parameters directly. "
    "Required: `attempts` (non-empty list of {`name` (optional; defaults to "
    "`attempt-{index}`, and every resolved name — explicit or defaulted — must "
    "be unique), `source` (non-empty script), `seed` (optional non-bool integer "
    "in the CP-SAT random_seed signed-int32 range), `config` (optional JSON "
    "object, default `{}`), `timeout_ms` (optional per-attempt override)}) and "
    "Optional: `objective_sense` ('maximize'|'minimize' for optimization; omit "
    "or pass null for feasibility), `default_timeout_ms` "
    "(fallback for attempts with no `timeout_ms`), `max_parallel_attempts` "
    "(default 1 = serial; capped at min(server CPU count, 4) and rejected above "
    "that), `problem` (forwarded to the checker payload), `checker` (a Python "
    "checker source string), `checker_timeout_ms` (defaults to the effective "
    "per-attempt timeout), `include_winner_stdout` (default `true`; pass "
    "`false` to omit the winner's raw `stdout` from the returned result — "
    "`solution`/`objective`, the parsed structured answer, are unaffected; "
    "for a well-behaved script `stdout` is a redundant raw-text copy of the "
    "same JSON). "
    "Two cooperative protocols, both OPT-IN for the attempt's script: `seed` "
    "sets `OPENCONSTRAINT_MCP_CPSAT_SEED`; a non-empty `config` is written to a "
    "temp JSON file and its path set as `OPENCONSTRAINT_MCP_CPSAT_CONFIG`. A "
    "script that ignores either env var simply runs unaffected — the server "
    "cannot force either into arbitrary Python. An empty `config` (`{}`) is "
    "identical to omitting it: no temp file, no env var, `config_sha256` null. "
    "PARALLELISM: attempts run through a bounded worker pool sized by "
    "`max_parallel_attempts`; coordinate it with each script's own "
    "`solver.parameters.num_workers` — `max_parallel_attempts * num_workers` "
    "oversubscribing the machine can make runs slower and less stable. When an "
    "attempt's `config` sets a `num_workers` key, the server checks "
    "`max_parallel_attempts * num_workers` against this machine's CPU count and "
    "adds an advisory entry to the result's `warnings` list if it's exceeded — "
    "a best-effort heuristic limited to that one cooperative convention; it "
    "cannot see `num_workers` set any other way (e.g. hardcoded in the "
    "script). Results "
    "are always returned in ORIGINAL attempt order, and winner tie-breaks use "
    "that same order, never completion order. "
    "Acceptance is two ordered gates: base acceptance (status "
    "`optimal`/`feasible`/`timeout`, a non-empty solution, and in optimization "
    "mode only a finite numeric objective), then — only for base-eligible "
    "attempts — the optional checker gate (accepted iff the checker returns "
    "`accepted`). In optimization mode, the winner is the accepted attempt with "
    "the best objective for `objective_sense`, ties broken by stronger status "
    "(optimal > feasible > timeout) then fastest `duration_ms` then earliest "
    "attempt order. In feasibility mode, objective is not required and winner "
    "selection uses stronger status then fastest `duration_ms` then earliest "
    "attempt order. "
    "BUDGET GATE: synchronous and rejected UP FRONT (before any child runs) "
    "when its projected wall-clock budget — batched by `max_parallel_attempts`, "
    "using each attempt's effective timeout, checker timeout when present, and "
    "a conservative per-child timeout/kill overhead — exceeds a fixed cap; "
    "reduce attempt count/timeouts or raise `max_parallel_attempts` to fit. A "
    "rejection's error message breaks the projected total down by the slowest "
    "attempt's own components (timeout, checker timeout, overhead) so the "
    "actual bottleneck is visible, not just the total. THIS TOOL IS FOR "
    "COMPARING MULTIPLE short/medium attempts in one call, not for running one "
    "long attempt — for a SINGLE attempt expected to approach or exceed this "
    "cap, use `run_cpsat_python` instead, which has no multi-attempt budget "
    "ceiling. "
    "Returns a CpsatPythonExperimentResult: `status` ('winner'|'no_winner'), "
    "`winner_index`/`winner_name`/`winner` (a full CpsatPythonResult, all "
    "present iff 'winner'), `attempts` (every attempt, accepted or not, with "
    "its resolved `name`, `source_sha256`, `config_sha256`), `elapsed_ms`, "
    "`objective_sense` (or null for feasibility), `selection_policy`, "
    "`source_sha256` (index-aligned with `attempts`), `checker_sha256`, "
    "`problem_sha256`, `warnings` (advisory strings: the num_workers-"
    "oversubscription check above when triggered, PLUS — whenever there is a "
    "winner — an unconditional reproducibility disclaimer; empty only when "
    "there is no winner and nothing else is flagged). "
    "REPRODUCIBILITY: an experiment winner is ONE OBSERVED RUN, not a "
    "guarantee — CP-SAT's randomized search, LNS, restarts, parallel "
    "portfolio search (num_workers > 1), and short time limits can all "
    "make a winner fail to reproduce its objective when "
    "`save_verified_cpsat_python` re-runs it fresh. For stronger "
    "reproducibility, set explicit solver parameters such as `random_seed`, "
    "consider `num_workers = 1`, and verify with the same timeout — "
    "exact determinism is still not guaranteed. A `timeout` winner is "
    "REPORTABLE, not SAVABLE — `save_verified_cpsat_python`'s reported gate "
    "still requires `optimal`/`feasible`. Pass this result's `experiment_result` "
    "to `save_verified_cpsat_python` to persist the winner with provenance; "
    "that save re-verifies the winner fresh and NEVER trusts this result as "
    "evidence. " + _CPSAT_CHILD_POSTURE
)

SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION = (
    "Re-run a CP-SAT Python script and persist it to a LOCAL directory when it "
    "passes the configured save gate(s). The server trusts no prior claim of "
    "success: it runs `source` again and saves only when all supplied gates pass. "
    "`target_dir` must be an EXPLICIT ABSOLUTE local directory whose parent "
    "exists; the server never opens a file dialog. "
    "Gate order (reported → expectation → checker): "
    "(1) Reported gate — always applied: `status` must be `optimal`/`feasible` "
    "AND `solution` must be non-empty. "
    "(2) Expectation gate — optional: supply `expectation` "
    "({`objective_sense`: 'maximize'|'minimize', `objective_threshold`: <number>}) "
    "to check that the script's reported objective meets a numeric threshold. "
    "This is a QUALITY GATE, not an optimality proof — it cannot certify that no "
    "better solution exists. "
    "(3) Checker gate — optional: supply `checker` (a Python script source string) "
    "to validate the solution against problem-specific constraints. The checker "
    "receives a payload JSON path as its first positional argument and must emit "
    "one JSON object as its final stdout line: "
    "{`status`: 'accepted'|'rejected'|'error', `errors`: [...], `details`: {...}}. "
    "Only `accepted` with an empty `errors` list passes. Supply `checker_timeout_ms` "
    "to override the checker's timeout (defaults to `timeout_ms`). "
    "Optional `seed` (a non-bool integer in the CP-SAT random_seed signed-int32 "
    "range) is a single-run replay aid: the re-run sets "
    "`OPENCONSTRAINT_MCP_CPSAT_SEED` so a cooperating script uses that seed, and "
    "the seed is recorded in the manifest. The save gates are UNCHANGED — the "
    "reported gate still requires `optimal`/`feasible`. To reproduce a seeded "
    "save by hand, set `OPENCONSTRAINT_MCP_CPSAT_SEED` to the recorded seed; the "
    "saved `solution.py` is byte-for-byte the script and carries only its own "
    "seed fallback. "
    "Optional `config` (a JSON object, `{}` treated identically to omitted) is "
    "the same kind of replay aid: the re-run writes it to a temp file and sets "
    "`OPENCONSTRAINT_MCP_CPSAT_CONFIG`, then persists it as `replay-config.json` "
    "on a successful save. Optional `experiment_result` (a CpsatPythonExperimentResult "
    "from `run_cpsat_python_experiment`) is PROVENANCE ONLY — never verification "
    "evidence. When supplied it must be self-consistent with this save request "
    "(`status=='winner'` — i.e. the experiment produced at least one accepted "
    "attempt — and at least one ACCEPTED attempt in `experiment_result.attempts` "
    "whose `source_sha256` matches `source`, `seed` matches the supplied `seed`, "
    "and `config_sha256` matches the canonical hash of the supplied `config`; "
    "not necessarily the experiment's own `winner_index`) or the save is "
    "REJECTED before any child runs; the fresh re-run and gates below still "
    "decide everything. On a successful save its full attempt table is "
    "written as `experiment-log.json` — "
    "a provenance SUMMARY (hashes and scalar outcomes per attempt), never an "
    "archive of every attempt's full `config`; only the saved attempt's own config "
    "is persisted, via `replay-config.json`. Saved seed/config provenance improves "
    "replayability but does not guarantee bit-for-bit reproducibility — CP-SAT "
    "randomness, parallel search, and script-level nondeterminism can still differ; "
    "this fresh save-time run is the authority. "
    "Fixed filenames: `solution.py` (the script); `problem.txt` when `problem` is "
    "supplied; `checker.py` and `solution.json` when a checker is supplied; "
    "`replay-config.json` when `config` is supplied; `experiment-log.json` when "
    "`experiment_result` is supplied; and a `.openconstraint-model.json` manifest "
    "recording tool version, timestamp, verification level, expectation settings, "
    "checker summary (status/error_count/duration/timed_out/truncated only — no "
    "free text), a compact experiment-log summary, and per-file sha256 hashes. "
    "Overwrite is MARKER-GATED: a new or empty path is written directly; a "
    "non-empty directory is replaced wholesale (staged sibling + atomic swap) "
    "only when it holds a prior save's manifest marker, `overwrite=true` is "
    "passed, and it contains no files the prior save did not write; anything "
    "else is refused with an actionable error and nothing is touched. "
    "Returns a SaveVerifiedPythonResult with: `saved` (bool), "
    "`verification_level` ('none'|'reported'|'expectation'|'checked' — the "
    "highest gate that passed; combine with `saved` to distinguish a saved result "
    "from a failed gate at the same level), `reported_passed` (bool), "
    "`expectation` (echoed expectation or null), `expectation_passed` "
    "(bool or null when not evaluated), `checker` (CpsatCheckerReport or null), "
    "`status`, `target_dir`, `reason`, `solution`, `objective`, `stdout`, "
    "`stderr`, `timed_out`, `truncated`, `duration_ms`, `files`. "
    "A failed gate returns `saved=False` with `reason` and writes NOTHING. "
    "CP-SAT's nondeterminism may yield a different (but still valid) solution "
    "from the prior run; the save gate checks status, not solution-equality. "
    + _CPSAT_CHILD_POSTURE
)

SOLVE_CPSAT_PYTHON_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through writing a CP-SAT Python script "
    "that conforms to the run_cpsat_python output contract and running it "
    "via run_cpsat_python. Use when the user's problem is better expressed "
    "in Python than in MiniZinc (custom data structures, imperative "
    "pre-processing, NumPy-style indexing)."
)

# --- CP-SAT background job descriptions -------------------------------------

SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Submit an OR-Tools CP-SAT Python INLINE SOURCE as a BACKGROUND JOB and return "
    "immediately, so a long solve cannot hit a synchronous MCP client timeout. "
    "Takes the same `source` and `timeout_ms` as `run_cpsat_python`. "
    "The script must conform to the run_cpsat_python output contract: emit a single "
    'JSON object to stdout as its last line (`{"status": "<status>", "objective": '
    '<float|null>, "solution": {<str: val>}}`). '
    "Returns a CpsatPythonJobStatus with a server-generated opaque `job_id` and "
    "an initial `state` of `queued` or `running` (a very fast job may already be "
    "terminal); poll with `get_cpsat_python_job(job_id)` and "
    "stop with `cancel_cpsat_python_job(job_id)`. "
    "Admission is BOUNDED: at most a fixed number of CP-SAT jobs run at once, "
    "further submits sit `queued` up to a cap, and a submit beyond that is REJECTED "
    "with an MCP error (retry once a running job finishes). "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_cpsat_python_job")
    + _CPSAT_CHILD_POSTURE
)

SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION = (
    "Submit a LOCAL OR-Tools CP-SAT Python SCRIPT FILE as a BACKGROUND JOB and "
    "return immediately — the path-based counterpart to `submit_cpsat_python_job`. "
    "Pass `script_path` (an absolute local path) instead of pasting source; the "
    "script runs with its working directory set to the file's own directory so "
    "relative imports and data-file opens resolve. "
    "`script_path` is validated before admission: a missing path, a non-file, an "
    "empty/whitespace-only script, or non-UTF-8 content is rejected with an "
    "actionable MCP error and no job is created. "
    "Same output contract as `run_cpsat_python_file`; same admission bounds, "
    "polling (`get_cpsat_python_job`), and cancel (`cancel_cpsat_python_job`) as "
    "`submit_cpsat_python_job` — the `job_id` is kind-agnostic. "
    + _REGISTRY_NOTE
    + " "
    + _returns_immediately_note("get_cpsat_python_job")
    + _CPSAT_CHILD_POSTURE
)

GET_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Poll a background CP-SAT Python job by its `job_id` (from "
    "`submit_cpsat_python_job` or `submit_cpsat_python_file_job`). "
    "Returns a CpsatPythonJobStatus: `job_id`, `state`, `timeout_ms`, "
    "`submitted_at_ms`, `started_at_ms`, `finished_at_ms`, `elapsed_ms`, an "
    "optional `result` (the full CpsatPythonResult), and an optional `message`. "
    "`state` is one of `queued`, `running`, `succeeded`, `failed`, `timeout`, "
    "`cancelled`. CONTRACT: `result` is present exactly when `state` is "
    "`succeeded` or `timeout`, absent for `queued`/`running`/`failed`/`cancelled` "
    "— so branch on `state`, not on `result`. `failed` means the job machinery "
    "itself raised (no result, see `message`); a script-level `error` verdict "
    '(e.g. a crash or bad JSON) is a `succeeded` job whose `result.status == "error"`, '
    "NOT `failed`. A `timeout` job carries its partial CpsatPythonResult "
    "(`result.timed_out == True`, best-so-far `solution`/`objective`). While "
    "`running`, only `state` + `elapsed_ms` advance. PACE polling against the "
    "job's budget: a `running` job has roughly `timeout_ms - elapsed_ms` left; "
    "wait a fraction of the remaining budget between polls rather than looping "
    "tightly. On `succeeded` or `timeout`, present the result as "
    "`run_cpsat_python` requires: lead with the plain-language status and the "
    "solution in the user's terms. " + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION = (
    "Request cancellation of a background CP-SAT Python job by `job_id`. A job "
    "still `queued` is dropped before it starts; a `running` job has its Python "
    "child process tree terminated. "
    + _cancellation_idempotent_note("`succeeded`/`failed`/`timeout`/`cancelled`")
    + "Returns the CpsatPythonJobStatus; the job reaches `cancelled` (with "
    "`result is None`) once the worker observes the request — poll "
    "`get_cpsat_python_job` to confirm the terminal state. " + _UNKNOWN_JOB_ID_ERROR
)

LIST_CPSAT_PYTHON_JOBS_DESCRIPTION = (
    "List the currently retained background CP-SAT Python jobs as "
    "CpsatPythonJobStatus entries (one per job), covering every state from "
    "`queued` to terminal. Works for both inline-source and file-based jobs. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)
