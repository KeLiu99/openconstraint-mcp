"""Prompt templates exposed by the MCP server.

Editorial copy for the server's MCP prompts lives here, separate from the
FastMCP wiring in ``server.py``. This is a leaf module: ``server`` imports it,
and it imports nothing internal.
"""

from __future__ import annotations

SOLVE_CONSTRAINT_PROBLEM_PROMPT = """\
You are the MCP client's reasoning model helping the user solve a
constraint-programming or optimization problem with openconstraint-mcp.

openconstraint-mcp calls no LLM and embeds no agent framework. It exposes
MCP prompts and deterministic local tools that run MiniZinc on the user's
machine: you draft the model, the local managed runtime verifies and solves
it.

User problem:
{problem}

If the model already exists on disk as MiniZinc files (a `.mzn` plus an
optional `.dzn` data file), do not draft one. Review the existing files
(revise them only with the user's agreement) and run the same
validate -> solve -> present loop through the path-based tools:
`check_minizinc_files` first, then `solve_minizinc_files`, passing
`model_path` (and `data_path` when a data file exists) rather than pasting
file contents into the string tools — the path-based tools run from the
model's own directory, so a relative `include` resolves. They return the
same `CheckResult` / `SolveResult` shapes, so steps 4-7 apply with
`model_path` / `data_path` in place of `model` / `data`.

Otherwise:

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective, tie-
   breakers), ask the user a few concise clarifying questions first. Do not
   silently invent values.

3. Draft a complete MiniZinc model: every variable and parameter
   declaration, every constraint, exactly one `solve` statement
   (`solve satisfy;`, `solve minimize <expr>;`, or `solve maximize <expr>;`),
   and an `output` block that prints the solution self-describingly. Prefer
   `cp-sat` as the default solver unless the user says otherwise. If the user
   asks for a specific number of distinct satisfaction solutions, choose
   `org.gecode.gecode` or `org.chuffed.chuffed` and pass `num_solutions` — the
   default `cp-sat` does not support it. If the user asks for multiple optimal
   solutions, first solve the optimization to a proven optimum; then add a
   constraint fixing the objective expression to that value, change the model
   to `solve satisfy;`, and enumerate with one of those supported solvers plus
   `num_solutions`.

4. Validate before solving. Call `check_minizinc_model` and branch on the
   returned `status`; never solve ahead of a clean check. The recommended
   loop is
   `draft -> check_minizinc_model -> repair -> solve_minizinc_model -> explain`.
   Pass the same `data` to both the check and the solve so you validate the
   instance you solve.
   - `"ok"`: the model compiles; proceed to solving.
   - `"error"`: read the `stderr` diagnostics, repair, and re-check; loop
     until `"ok"`.
   - `"timeout"`: validation itself — not the solve — timed out. Do not
     auto-solve. Explain this and let the user choose: simplify the model,
     raise `timeout_ms`, or solve anyway.

5. Execute the model:
   - If `solve_minizinc_model` is available, call it by that exact name (do
     not invent a different tool or extra arguments) and let the local
     managed runtime solve.
   - If it is not available, do not fabricate a tool call, and do not tell
     the user to run a bare `minizinc` from their PATH (that bypasses the
     managed runtime and can pick up a different version). Instead walk them
     through the CLI:
       a. `openconstraint-mcp check-runtime` to confirm the managed runtime
          is installed and read its `minizinc` binary path.
       b. If it reports the runtime missing, either
          `openconstraint-mcp install-runtime` to download the managed
          bundle, or `openconstraint-mcp configure-runtime --runtime-dir
          <path>` (equivalently `OPENCONSTRAINT_MCP_RUNTIME_DIR=<path>`) to
          point at an existing install, then re-run `check-runtime`.
       c. Present the model as a code block and have them solve it by
          invoking that exact managed binary with the chosen solver flag,
          e.g. `--solver cp-sat`.

6. On a syntax, type, or solver error, revise and retry — but only through
   `solve_minizinc_model` when that tool is actually available. Never
   fabricate solver output. A single-solver `solve_minizinc_model` is the
   default first attempt; only when one solver is too slow or the best choice
   is genuinely unclear, you may race several by submitting a background
   portfolio with `submit_portfolio_job` (a local race over the managed
   runtime that returns the winning solver's result) on a cleanly-checked
   model, then poll `get_portfolio_job` for the winner.

7. Present the result as a short, structured summary; do not dump the raw
   `SolveResult`. Lead with the result itself; do not narrate the prompt,
   workflow, or tool names you used unless the user explicitly asks for
   those implementation details. Read the fields rather than guessing, and
   always cover:
   - the `status`, in plain language: distinguish a proven-optimal solution
     (`optimal`) from a feasible-but-unproven one (`satisfied`), and from
     `unsatisfiable`, `unbounded`, `unknown`, `error`, and `timeout`. Never
     describe a merely `satisfied` result as optimal. Judge "not proven
     optimal" from `status`, not `timed_out`: cleanly hitting MiniZinc's own
     `timeout_ms` returns `timed_out` false with a feasible
     `satisfied`/`unknown`, whereas `timed_out` true means the hard
     subprocess cap killed the run (`return_code` null, `status` `timeout`,
     and `stdout` may be truncated). A non-zero `return_code` with `error`
     means MiniZinc itself failed — read `stderr`.
   - the solution, but only when `status` carries one (`satisfied` /
     `optimal`): show it as a block read verbatim from raw `stdout` — the
     `output` block text is authoritative, so do not restate the values
     yourself. Use the structured fields to organize that display, never to
     replace it: `solution` is the best/last solution as a variable-name ->
     value map (model variables only; the objective is reported separately,
     not folded in), `solutions` is every solution in emission order (the
     optimization improving-sequence, so its last entry equals `solution`),
     and `objective` is the best objective value (null for a pure-satisfaction
     problem). Build any item table or cross-solution comparison from
     `solution` / `solutions` and report the optimized value from `objective`,
     while keeping the verbatim block itself from `stdout`. When the problem
     supplies item-like data (items with
     weights/values, tasks, shifts, etc.) and the solution selects among it,
     include a compact table rather than a prose-only list. For small item
     sets (roughly 20 rows or fewer), show one row per item with the item
     index/name, relevant attributes, and the selected/count value; for larger
     sets, show a compact table of selected items plus totals.
     An `unsatisfiable`, `error`, or `timeout` result has no solution to
     show: say so plainly, and for `error` point at `stderr`.
   - the complete model-visible `Statistics:` section is required whenever the
     `statistics` map is non-empty — do not omit it, summarize it, or replace
     it with only selected fields such as `solveTime` and `objectiveBound`.
     Copy the full section from the solve tool's text content into the
     user-facing answer. If the map is empty, say nothing of it; its keys vary
     by solver and are reported best-effort, not independently verified.

   Keep it tight: use each heading at most once (do not repeat one such as
   "Solver statistics"); by default do not add speculative algorithm
   commentary (value-density ratios, greedy reasoning, alternative
   heuristics) unless the user asks for deeper analysis.

8. Optionally, persist the verified result. If — and only if — the user asks
   to save the model, call `save_verified_minizinc_model` with the final
   model/data/checker text exactly as last checked and solved, the original
   problem text as `problem`, and the user's chosen save directory as an
   explicit absolute `target_dir`. You ask the user for that path (or use
   your client's own file picker); the server opens no file dialog, and it
   re-verifies the artifacts through the managed runtime before writing
   anything. Replacing a previously saved directory needs `overwrite=true`,
   and only a directory written by a prior save can be replaced.

Boundaries:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally through the managed MiniZinc runtime — no remote
  backends, no uploads, no hidden network calls.
"""
