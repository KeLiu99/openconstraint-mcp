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
    "check_minizinc_model, then solve with solve_minizinc_model. When the "
    "model (and any data) already exist as local files, pass their paths to "
    "check_minizinc_files / solve_minizinc_files instead of reading the files "
    "into the string tools — the path-based tools run from the model's own "
    "directory, so a relative `include` resolves. Either way, lead with the "
    "result: a plain-language status, the stdout solution stated in the "
    "terms of the user's problem (not the raw JSON SolveResult), a compact "
    "item table when the problem supplies item-like data, and the complete "
    "model-visible `Statistics:` section when present. Do not condense it to "
    "selected fields. All execution must use the managed local MiniZinc "
    "runtime; do not use remote solvers or a bare PATH minizinc."
)

CHECK_RUNTIME_DESC = "Report whether the managed MiniZinc runtime is installed."

LIST_AVAILABLE_SOLVERS_DESC = "List solvers available in the managed MiniZinc runtime."

SOLVE_MINIZINC_MODEL_DESC = (
    "Solve a complete MiniZinc model through the managed local runtime. "
    "`model` must be full source: declarations, constraints, exactly one "
    "`solve` statement, and an `output` block. Optional `data` is `.dzn` "
    "text, run as a data file beside the model (omit it when the model "
    "needs none). Returns a SolveResult: `status`, `solver`, `return_code` "
    "(null on a subprocess timeout), `timed_out`, raw `stdout`/`stderr` (so "
    "you can revise and retry on errors), `elapsed_ms`, and a best-effort "
    "`statistics` map parsed from `%%%mzn-stat:` lines (may be empty; keys "
    "are solver-defined; raw `stdout` stays authoritative). The model-visible "
    "text content includes a `Statistics:` section whenever that map is "
    "non-empty, with an explicit final-answer requirement not to omit it; "
    "copy that entire section into the user-facing answer rather than "
    "summarizing selected fields. `structuredContent` carries the complete "
    "SolveResult."
)

CHECK_MINIZINC_MODEL_DESC = (
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

FIND_UNSAT_CORE_DESC = (
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
_FILE_TOOL_SHARED_DESC = (
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

CHECK_MINIZINC_FILES_DESC = (
    "Compile-check a MiniZinc model from local file paths WITHOUT solving "
    "it — the path-based sibling of `check_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESC
    + " Returns the same CheckResult shape (`status` "
    "`ok`/`error`/`timeout`, `solver`, `stdout`, `stderr`, `elapsed_ms`); "
    "`ok` means it compiles, not that it is satisfiable."
)

SOLVE_MINIZINC_FILES_DESC = (
    "Solve a MiniZinc model from local file paths — the path-based sibling "
    "of `solve_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESC
    + " Returns the same SolveResult shape (`status`, `solver`, "
    "`return_code`, `timed_out`, `stdout`, `stderr`, `elapsed_ms`, "
    "`statistics`) and the same model-visible `Statistics:` summary whenever "
    "the parsed map is non-empty, with an explicit final-answer requirement "
    "to copy the entire section rather than summarizing selected fields."
)

FIND_UNSAT_CORE_FILES_DESC = (
    "Diagnose an unsatisfiable MiniZinc model from local file paths by "
    "computing a minimal unsatisfiable subset (MUS) via the managed "
    "runtime's findMUS tool — the path-based sibling of `find_unsat_core`. "
    + _FILE_TOOL_SHARED_DESC
    + " Returns the same UnsatCoreResult shape (`status` "
    "`mus_found`/`no_core`/`error`/`timeout`, `core`, `message`, `stdout`, "
    "`stderr`, `elapsed_ms`). `core` resolves from the ENTRY MODEL FILE "
    "only, so a MUS member in an INCLUDED file appears in authoritative "
    "`stdout` but NOT in `core`. The subset is MINIMAL but not necessarily "
    "the globally smallest."
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESC = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc)."
)
