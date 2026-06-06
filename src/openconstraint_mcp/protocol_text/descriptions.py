"""MCP tool and prompt description strings.

These are protocol-contract texts — what MCP clients see as tool/prompt
documentation.  Keeping them here lets server.py focus on wiring.
"""

MCP_SERVER_INSTRUCTIONS = (
    "Use this MCP server for local constraint programming and optimization: "
    "MiniZinc and CP-SAT models for scheduling, rostering, knapsack, "
    "allocation, assignment, routing, bin-packing, SAT/UNSAT analysis, model "
    "validation, and solver statistics. For natural-language problems, prefer "
    "the solve_constraint_problem prompt when the client supports MCP prompts; "
    "otherwise draft MiniZinc in the client LLM, check it with "
    "check_minizinc_model, then solve with solve_minizinc_model. To discover "
    "which parameters a model needs as data before building a `.dzn` (and its "
    "output variables and solve method), inspect its interface with "
    "inspect_minizinc_model without solving. When the "
    "model (and any data) already exist as local files, pass their paths to "
    "check_minizinc_files / solve_minizinc_files instead of reading the files "
    "into the string tools — the path-based tools run from the model's own "
    "directory, so a relative `include` resolves. Either way, lead with the "
    "result: a plain-language status, the stdout solution stated in the "
    "terms of the user's problem (not the raw JSON SolveResult), a compact "
    "item table when the problem supplies item-like data, and the complete "
    "model-visible `Statistics:` section when present. Do not condense it to "
    "selected fields. Use `num_solutions` only with `org.gecode.gecode` or "
    "`org.chuffed.chuffed`, not the default `cp-sat`; for multiple optimal "
    "solutions, solve the optimization first, then re-solve as satisfaction "
    "with the objective fixed to the proven optimum. All execution must use "
    "the managed local MiniZinc "
    "runtime; do not use remote solvers or a bare PATH minizinc."
)

CHECK_RUNTIME_DESCRIPTION = "Report whether the managed MiniZinc runtime is installed."

LIST_AVAILABLE_SOLVERS_DESCRIPTION = (
    "List solvers available in the managed MiniZinc runtime. Returns a "
    "SolverList of SolverInfo entries — each with `id`, `name`, `version`, "
    "`tags`, and a `capabilities` object of deterministic facts read from the "
    "managed runtime's own `--solvers-json` config, for client-side solver "
    "routing. `capabilities.supports_all_solutions` (`-a`), `supports_free_search` "
    "(`-f`), `supports_parallel` (`-p`), and `supports_random_seed` (`-r`) report "
    "membership in the solver's declared `stdFlags`. `supports_num_solutions` "
    "(`-n`) is NOT a raw stdFlags read: it is the conservative canonical gate "
    "matching the `num_solutions` solve control — True only for "
    "`org.gecode.gecode` and `org.chuffed.chuffed`, not the default `cp-sat`. "
    "Capabilities are advisory facts, not enforcement: the server still does not "
    "reject `-a/-f/-p/-r` at solve time. The raw advisory `std_flags` list reports "
    "the standard flags the solver configuration declares; it is NOT a passthrough "
    "surface — a client cannot send those flags back into `solve_minizinc_model` / "
    "`solve_minizinc_files`. Two cases to keep distinct: (a) `std_flags` may list "
    "standard flags the server exposes no named control for at all (e.g. `-i`, "
    "`-s`, `-t`, `-v`) — purely informational; and (b) the allowlist divergence — "
    "`org.gecode.gist` lists `-n` (which does map to the named `num_solutions` "
    "control) yet `supports_num_solutions` is False, because the conservative gate "
    "excludes gist. The model-visible text content presents a complete "
    "`id`/`name`/`version` inventory table of every solver with a final-answer "
    "requirement to copy the table without omitting rows, converting it to "
    "bullets/prose, summarizing, or grouping entries, followed by a user-visible "
    "note that detailed solver capabilities can be requested. "
    "The structured `SolverList` also carries that top-level `capability_note`. "
    "The full `capabilities` metadata (the `supports_*` booleans and `std_flags`) "
    "lives in the structured result and is not printed by default — request it "
    "explicitly to surface it."
)

SOLVE_MINIZINC_MODEL_DESCRIPTION = (
    "Solve a complete MiniZinc model through the managed local runtime. "
    "`model` must be full source: declarations, constraints, exactly one "
    "`solve` statement, and an `output` block. Optional `data` is `.dzn` "
    "text, run as a data file beside the model (omit it when the model "
    "needs none). Returns a SolveResult: `status`, `solver`, `return_code` "
    "(null on a subprocess timeout), `timed_out`, `elapsed_ms`, `stdout` "
    "(the human-readable solution text, reconstructed from the solve "
    "stream's output sections), `stderr` (the run's diagnostic channel — "
    "model/solver errors and warnings, so you can revise and retry), a "
    "structured `solution` (the best/last solution as a variable -> value "
    "map, model variables only), `solutions` (every solution in order; its "
    "last entry is `solution`), `objective` (the best objective value, null "
    "for a pure-satisfaction problem), and a best-effort `statistics` map "
    "(may be empty; keys are solver-defined). The structured values are "
    "emitted by the runtime's machine-readable solve stream, not scraped "
    "from text. The model-visible text content includes a `Statistics:` "
    "section whenever that map is non-empty, with an explicit final-answer "
    "requirement not to omit it; copy that entire section into the "
    "user-facing answer rather than summarizing selected fields. Optional "
    "solver/search controls (all default to current behavior, solve-only): "
    "`free_search` (bool -> `-f`: let the solver use its own search instead "
    "of the model's search annotations — solver-dependent, not 'no search'); "
    "`parallel` (int >= 1 -> `-p`: parallel search threads); `random_seed` "
    "(int -> `-r`); `all_solutions` (bool -> `-a`: enumerate all solutions, "
    "or the optimization improving-sequence, into `solutions`); "
    "`num_solutions` (int >= 1 -> `-n`: cap the number of solutions for a "
    "SATISFACTION problem; SOLVER-GATED — only `org.gecode.gecode` or "
    "`org.chuffed.chuffed`, NOT the default `cp-sat` (any other solver returns "
    "an actionable error); not meaningful for optimization — use "
    "`all_solutions` there). "
    "`structuredContent` carries the complete SolveResult."
)

CHECK_MINIZINC_MODEL_DESCRIPTION = (
    "Compile-check a complete MiniZinc model through the managed local "
    "runtime WITHOUT solving it — flattening it for the chosen solver to "
    "catch syntax, type, missing-include, invalid-domain, and "
    "unsupported-construct errors so you can repair it before calling "
    "`solve_minizinc_model`. Optional `data` is `.dzn` text; a "
    "parameterized model needs the same `data` you will pass to the solve "
    "in order to flatten (omit it when the model needs none). Returns a "
    "CheckResult: `status` (`ok`/`error`/`timeout`), `solver`, raw "
    "`stdout`/`stderr`, `elapsed_ms`. `ok` means it compiles, not that it "
    "is satisfiable."
)

INSPECT_MINIZINC_MODEL_DESCRIPTION = (
    "Inspect a MiniZinc model's INTERFACE through the managed local runtime "
    "WITHOUT solving it — report which parameters it needs as data, which "
    "variables it outputs, their types (array `dim`, set-ness), and the solve "
    "`method` (`sat`/`min`/`max`), so you can build correct `.dzn` data before "
    "spending a solve. Optional `data` is `.dzn` text run beside the model "
    "(omit it when the model needs none). Returns a ModelInspectionResult: "
    "`status` (`ok`/`error`/`timeout`), `solver`, raw `stdout`/`stderr`, "
    "`elapsed_ms`, and — only when `ok` — a structured `interface` with "
    "`method`, `required_parameters`, `output_variables`, `has_output_item`, "
    "`globals`, `included_files`. `required_parameters` is the set of "
    "parameters STILL needing a value given any `data` you passed: with no "
    "data it is the model's full required set; supplying the matching data "
    "shrinks it, and an empty `required_parameters` means the data is "
    'complete. IMPORTANT: `status="ok"` means only that the interface was '
    "extracted — it is NOT a data-completeness signal; only "
    "`required_parameters == {}` is. `output_variables` is advisory (the "
    "model's output variables, not necessarily every decision variable). "
    "Enum-typed entries appear as `int`; enum names are not surfaced in v1."
)

FIND_UNSAT_CORE_DESCRIPTION = (
    "Diagnose an unsatisfiable MiniZinc model by computing a minimal "
    "unsatisfiable subset (MUS) of its constraints via the managed "
    "runtime's findMUS tool. Use it when solve_minizinc_model returns "
    "'unsatisfiable'. Optional `data` is `.dzn` text; pass the SAME `data` "
    "you solved with (omit it when the model needs none). Returns an "
    "UnsatCoreResult: `status` "
    "(`mus_found`/`no_core`/`error`/`timeout`), `core`, `message`, raw "
    "`stdout`/`stderr`, `elapsed_ms`. `core` is a best-effort structured "
    "list (source span + text) resolved from the MODEL FILE only — a "
    "decision variable assigned in `data` acts as a constraint, so a MUS "
    "member can originate in the data file and appear in authoritative "
    "`stdout` but not in `core`. The subset is MINIMAL, but not necessarily "
    "the globally smallest, and a model may have several."
)

# Shared guidance injected into each path-based file-tool description.
_FILE_TOOL_SHARED_DESCRIPTION = (
    "Reads the model (and optional data) from local FILE PATHS on the "
    "machine running the server and runs the managed runtime from the "
    "model's own directory, so a relative `include` resolves just like a "
    "hand-run `minizinc`. `model_path` is a required `.mzn` path (must "
    "exist and be a regular file); `data_path` is an optional `.dzn` path. "
    "Paths resolve to absolute (prefer absolute); a missing/non-file, "
    "empty, or non-UTF-8 model is a clear MCP error before any run. It "
    "reads the model, the optional data, and any `include`d files; it never "
    "writes files, makes network calls, uploads data, or uses a remote "
    "solver."
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
    "`return_code`, `timed_out`, `elapsed_ms`, `stdout`, `stderr`, "
    "`solution`, `solutions`, `objective`, `statistics`) and the same "
    "model-visible `Statistics:` summary whenever the parsed map is "
    "non-empty, with an explicit final-answer requirement to copy the entire "
    "section rather than summarizing selected fields. Accepts the same "
    "optional solver/search controls as `solve_minizinc_model` "
    "(`free_search`, `parallel`, `random_seed`, `all_solutions`, and the "
    "solver-gated, satisfaction-only `num_solutions`)."
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

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc)."
)
