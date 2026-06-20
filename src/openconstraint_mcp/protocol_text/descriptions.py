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

_CPSAT_NATIVE = (
    "native in-process OR-Tools CP-SAT — no MiniZinc runtime, no subprocess, no network."
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
    "For structured/common problems, prefer solve_ortools_model "
    "(native in-process Google OR-Tools CP-SAT, no MiniZinc runtime needed — "
    "zero-install, still local and offline); for richer expressiveness or an "
    "independent verification pass, use MiniZinc."
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
    "`structuredContent` carries the complete SolveResult."
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
    "supplies the path. Fixed filenames: `model.mzn`; `data.dzn`, "
    "`checker.mzc.mzn`, and `problem.md` only when `data`, `checker`, and "
    "`problem` (the user's original natural-language text, saved only when "
    "passed) are supplied; `solve-result.json` (the verifying SolveResult); and "
    "a `.openconstraint-model.json` manifest recording tool version, timestamp, "
    "solver, the solve controls used, a verification summary, and per-file "
    "sha256 hashes. Overwrite is MARKER-GATED: a new or empty path is written "
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
    "results and writes NOTHING; argument/path problems are MCP errors. "
    + _LOCAL_ONLY_GUARANTEE
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
    "poll `get_solve_job` to confirm the terminal state. "
    + _UNKNOWN_JOB_ID_ERROR
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
    "is `models[attempts[winner_index].model_index]`. "
    + _UNKNOWN_JOB_ID_ERROR
)

CANCEL_PORTFOLIO_JOB_DESCRIPTION = (
    "Request cancellation of a background portfolio job by `job_id`, stopping the race "
    "AND every still-running attempt (each attempt's managed MiniZinc process tree is "
    "terminated). "
    + _cancellation_idempotent_note("`succeeded`/`cancelled`")
    + "Returns the PortfolioJobStatus; the job reaches "
    "`cancelled` (with `result is None`) once the race observes the request — poll "
    "`get_portfolio_job` to confirm the terminal state. "
    + _UNKNOWN_JOB_ID_ERROR
)

LIST_PORTFOLIO_JOBS_DESCRIPTION = (
    "List the currently retained background portfolio jobs as PortfolioJobStatus "
    "entries (one per job), covering `running` and the terminal states. "
    + _REGISTRY_NOTE
    + " "
    + _NO_ARGS_LIST_TOOL
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc)."
)

SOLVE_BUDGET_ALLOCATION_DESCRIPTION = (
    "Solve a high-level budget allocation / knapsack problem using "
    + _CPSAT_NATIVE
    + " Takes a ``SolveBudgetAllocationRequest`` (``items`` with cost/value/"
    "resource-usage/dependencies/conflicts, per-resource ``budgets``, optional "
    "value/cost/count bounds) and an ``objective`` of ``maximize_value`` | "
    "``maximize_count`` | ``minimize_cost``. Float costs and values are scaled to "
    "integers internally (a power of ten preserving each input's precision) so "
    "results stay exact. Returns the selected items, totals, per-budget "
    "usage/slack, optimality gap, and an explanation. Use for structured "
    "knapsack / capital-budgeting / portfolio-selection problems. "
    + _LOCAL_ONLY_GUARANTEE.replace("managed runtime", "OR-Tools CP-SAT (in-process)")
)

SOLVE_ASSIGNMENT_PROBLEM_DESCRIPTION = (
    "Solve a high-level task-to-agent assignment problem using "
    + _CPSAT_NATIVE
    + " Takes a ``SolveAssignmentProblemRequest`` (``agents`` with capacity/skills/"
    "cost-multiplier, ``tasks`` with required-skills/duration/priority, optional "
    "``cost_matrix`` [task_i][agent_j], ``force_assign_all``) and an ``objective`` "
    "of ``minimize_cost`` | ``maximize_assignments`` | ``balance_load``. Skill "
    "mismatches are forbidden, capacity caps enforced, and float costs scaled to "
    "integers (x100). Returns assignments (task→agent, cost), unassigned tasks, "
    "per-agent load, total cost, optimality gap, and an explanation. "
    + _LOCAL_ONLY_GUARANTEE.replace("managed runtime", "OR-Tools CP-SAT (in-process)")
)

SOLVE_SCHEDULING_PROBLEM_DESCRIPTION = (
    "Solve a high-level scheduling problem using "
    + _CPSAT_NATIVE
    + " Currently supports the ``minimize_makespan`` objective only "
    "(``minimize_cost``/``minimize_lateness`` return a clear ``ValueError``). Takes "
    "a ``SolveSchedulingProblemRequest`` (``tasks`` with duration/resource-usage/"
    "dependencies/earliest-start/deadline/priority, ``resources`` with "
    "capacity/cost, an optional ``max_makespan`` bound, ``no_overlap_tasks`` "
    "groups). Returns the makespan, the per-task schedule (start/end), optimality "
    "gap, and an explanation. "
    + _LOCAL_ONLY_GUARANTEE.replace("managed runtime", "OR-Tools CP-SAT (in-process)")
)

SOLVE_ROUTING_PROBLEM_DESCRIPTION = (
    "Solve a high-level routing problem using "
    + _CPSAT_NATIVE
    + " Currently supports single-vehicle TSP via circuit constraints; "
    "multi-vehicle VRP is deferred and raises a clear ``ValueError``. Takes a "
    "``SolveRoutingProblemRequest`` (``locations`` with optional ``coordinates`` "
    "for Euclidean distance, service-time/time-window/demand, an optional "
    "``distance_matrix``, optional ``vehicles``, ``force_visit_all``) and the "
    "``minimize_distance`` objective. Returns the ordered route(s) and total "
    "distance, optimality gap, and an explanation. "
    + _LOCAL_ONLY_GUARANTEE.replace("managed runtime", "OR-Tools CP-SAT (in-process)")
)

SOLVE_ORTOOLS_MODEL_DESCRIPTION = (
    "Solve a structured constraint/optimization model using "
    + _CPSAT_NATIVE
    + " Takes an ``ORToolsSolveRequest`` with ``mode`` (satisfy/optimize), "
    "``variables`` (bool or integer domain), and optional ``constraints`` of nine "
    "kinds: ``linear`` (sum coef*var sense rhs), ``all_different``, ``element``, "
    "``table``, ``cumulative``, ``circuit``, ``no_overlap``, ``implication`` "
    "(references a **template-only** linear constraint — enforced only under its "
    "condition, not standalone), and ``reservoir``. For optimization pass an "
    "``objective`` (single, or a list for lexicographic multi-objective by "
    "``priority`` — smaller priority solved first); ``search`` exposes "
    "``timeout_ms``, ``num_workers``, ``random_seed``, ``max_solutions``, and "
    "``warm_start``. Returns ``solutions``, the (integer) objective value(s), "
    "optimality gap, and solve time. PREFER this over MiniZinc for "
    "structured/common/zero-install problems; prefer MiniZinc when you need richer "
    "expressiveness or an independent verification pass."
)
