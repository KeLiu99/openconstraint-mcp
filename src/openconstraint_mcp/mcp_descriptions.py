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
    "check_minizinc_model, solve with solve_minizinc_model, then lead with "
    "the result: a plain-language status, the stdout solution stated in the "
    "terms of the user's problem (not the raw JSON SolveResult), and a brief "
    "statistics summary when present. All execution must use the managed "
    "local MiniZinc runtime; do not use remote solvers or a bare PATH "
    "minizinc."
)

CHECK_RUNTIME_DESC = "Report whether the managed MiniZinc runtime is installed."

LIST_AVAILABLE_SOLVERS_DESC = "List solvers available in the managed MiniZinc runtime."

SOLVE_MINIZINC_MODEL_DESC = (
    "Run a complete MiniZinc model through the managed local MiniZinc "
    "runtime. The `model` argument must be complete MiniZinc source — "
    "declarations, constraints, exactly one `solve` statement, and an "
    "`output` block. The optional `data` argument supplies MiniZinc "
    "data (`.dzn` contents) as text, supplied to the runtime as a data "
    "file alongside the model; omit it for models that need no external "
    "data. Returns a SolveResult with the run's `status`, the requested "
    "`solver`, the subprocess `return_code` (null on a subprocess timeout) "
    "and a `timed_out` flag, the runtime's raw `stdout` and `stderr` (so "
    "the caller can revise and retry on MiniZinc errors), `elapsed_ms`, and "
    "a best-effort `statistics` map of `%%%mzn-stat:` key/value pairs parsed "
    "from `stdout` — may be empty, keys are solver- and version-defined, and "
    "raw `stdout` stays authoritative (enabling statistics adds `%`-comment "
    "stat lines to `stdout`)."
)

CHECK_MINIZINC_MODEL_DESC = (
    "Compile-check a complete MiniZinc model through the managed local "
    "MiniZinc runtime without solving it. Flattens (compiles) the "
    "`model` for the chosen solver — catching syntax, type, "
    "missing-include, invalid-domain, and unsupported-construct errors "
    "— and returns a CheckResult with the check's status plus the "
    "runtime's raw stdout and stderr, so the caller can repair the "
    "model before calling `solve_minizinc_model`. The optional `data` "
    "argument supplies MiniZinc data (`.dzn` contents) as text, supplied "
    "as a data file alongside the model; a parameterized model needs it "
    "to flatten, so pass the same `data` you will pass to "
    "`solve_minizinc_model`. Omit it "
    "for models that need no external data. A status of `ok` means the "
    "model compiles, not that it is satisfiable."
)

FIND_UNSAT_CORE_DESC = (
    "Diagnose why a MiniZinc model is unsatisfiable by computing a "
    "minimal unsatisfiable subset (MUS) of its constraints via the "
    "managed runtime's findMUS tool (org.minizinc.findmus). Use it when "
    "solve_minizinc_model returns status 'unsatisfiable' to localize the "
    "conflict. The optional `data` argument supplies MiniZinc data "
    "(`.dzn` contents) as text, supplied as a data file alongside the "
    "model; pass the SAME `data` you passed to the solve that proved "
    "unsat, or a parameterized model cannot flatten. Omit it for models "
    "that need no external data. "
    "Returns an UnsatCoreResult whose status is 'mus_found', "
    "'no_core' (findMUS finished without reporting a MUS), 'error' (see "
    "stderr), or 'timeout'. `core` is a best-effort structured list of the "
    "conflicting constraints (source span + text) resolved from the "
    "MODEL FILE only; `stdout` preserves findMUS's raw output verbatim and "
    "is authoritative (a decision variable assigned in `data` acts as a "
    "constraint, so a MUS member can originate in the data file and appear "
    "in stdout but not in `core`). The reported subset is MINIMAL — no "
    "constraint can be dropped while staying unsatisfiable — but NOT "
    "necessarily the globally smallest, and a model may have several."
)

# Shared guidance injected into each path-based file-tool description.
_FILE_TOOL_SHARED_DESC = (
    "Reads the model (and optional data) from local FILE PATHS on the "
    "machine running the server, then runs the managed runtime in MiniZinc "
    "CLI style — from the model's own directory — so a relative "
    '`include "helpers.mzn";` resolves against that directory, just like '
    "running `minizinc` by hand. `model_path` is a `.mzn` path (required; "
    "must exist and be a regular file); `data_path` is an optional `.dzn` "
    "path (an empty data file is allowed). Standard-library includes "
    "(`globals.mzn`, etc.) resolve as well. Paths are resolved to absolute "
    "before use (prefer absolute paths); a missing/non-file, empty, or "
    "non-UTF-8 model is a clear MCP error before any run. The tool reads "
    "the model file, the optional data file, and any local files they "
    "reference through `include`; it does NOT write files, make network "
    "calls, upload data, or use a remote solver."
)

CHECK_MINIZINC_FILES_DESC = (
    "Compile-check a MiniZinc model from local file paths through the "
    "managed runtime WITHOUT solving it — the path-based sibling of "
    "`check_minizinc_model`. " + _FILE_TOOL_SHARED_DESC + " Returns a "
    "CheckResult with the same shape as `check_minizinc_model` "
    "(`status` of `ok`/`error`/`timeout`, `solver`, `stdout`, "
    "`stderr`, `elapsed_ms`); `ok` means the model compiles, not that "
    "it is satisfiable."
)

SOLVE_MINIZINC_FILES_DESC = (
    "Run a MiniZinc model from local file paths through the managed "
    "runtime — the path-based sibling of `solve_minizinc_model`. "
    + _FILE_TOOL_SHARED_DESC
    + " Returns a SolveResult with the same "
    "shape as `solve_minizinc_model` (`status`, `solver`, `return_code`, "
    "`timed_out`, `stdout`, `stderr`, `elapsed_ms`, `statistics`)."
)

FIND_UNSAT_CORE_FILES_DESC = (
    "Diagnose why a MiniZinc model from local file paths is "
    "unsatisfiable by computing a minimal unsatisfiable subset (MUS) "
    "via the managed runtime's findMUS tool — the path-based sibling "
    "of `find_unsat_core`. " + _FILE_TOOL_SHARED_DESC + " Returns an "
    "UnsatCoreResult (`status` of `mus_found`/`no_core`/`error`/"
    "`timeout`, `core`, `message`, `stdout`, `stderr`, `elapsed_ms`). "
    "`core` is a BEST-EFFORT structured list resolved from the "
    "ENTRY MODEL FILE only; `stdout` is authoritative. A MUS member "
    "that lives in an INCLUDED file appears in `stdout` but NOT in "
    "`core` (the filter matches the entry model's basename). The "
    "reported subset is MINIMAL but not necessarily the globally "
    "smallest."
)

SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESC = (
    "Guide the MCP client's LLM through translating a natural-language "
    "constraint or optimization problem into MiniZinc and running it "
    "through the local managed runtime (via solve_minizinc_model when "
    "available, otherwise by walking the user through the "
    "openconstraint-mcp CLI to set up and invoke the managed runtime "
    "manually — never via a bare PATH-based minizinc)."
)
