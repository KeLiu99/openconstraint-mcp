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
   fabricate solver output. For a HARD problem — `status` comes back
   `unknown`, the solve times out, or the best modeling/solving choice is
   genuinely unclear — the best approach is rarely knowable in advance, so
   explore rather than settling for one run. On a cleanly-checked model,
   race alternatives with a background portfolio via `submit_portfolio_job`
   (poll `get_portfolio_job` for the winner and the full per-attempt table),
   trying any of: alternative model formulations (`models`, a list — e.g. a
   different variable encoding, added redundant constraints, or
   symmetry-breaking constraints), different `solvers`, different seeds
   (`seed_count` or an explicit `seeds` list), and search controls
   (`free_search`, `parallel`, and each attempt's `per_attempt_timeout_ms`
   budget).
   For an especially hard instance, also consider the OR-Tools CP-SAT
   Python path (`solve_cpsat_python` prompt, `run_cpsat_python`) for the
   same problem — neither backend dominates for every problem shape, and
   the server's structured results and checkers from both let you compare
   outcomes before committing to one.

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
   and only a directory written by a prior save can be replaced. If you
   explored via `submit_portfolio_job`, also pass that job's
   `PortfolioSolveResult` as `portfolio_result` so the winning race's full
   attempt table (every formulation/solver/seed tried, and why) is
   persisted alongside the saved model as `experiment-log.json` — the
   server still re-verifies independently and never trusts the attached
   result as proof.

Boundaries:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally through the managed MiniZinc runtime — no remote
  backends, no uploads, no hidden network calls.
"""

SOLVE_CPSAT_PYTHON_PROMPT = """\
You are the MCP client's reasoning model helping the user solve a
constraint-programming or optimization problem using OR-Tools CP-SAT Python
through openconstraint-mcp.

openconstraint-mcp calls no LLM. It exposes deterministic local tools that
execute Python scripts in a child process on the user's machine: you write
the CP-SAT script, `run_cpsat_python` runs it locally and returns a
structured result.

User problem:
{problem}

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective,
   tie-breakers), ask concise clarifying questions first. Do not silently
   invent values.

3. Write a complete, runnable OR-Tools CP-SAT Python script:
   - Import `from ortools.sat.python import cp_model` and `import json`.
   - Build the model with `cp_model.CpModel()`, declare variables, add
     constraints, set the objective.
   - Create a solver: `solver = cp_model.CpSolver()`.
   - For a REPRODUCIBLE saved artifact, READ the seed from the environment
     (falling back to 42) and prefer a single search worker:
       `import os`
       `solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))`
       `solver.parameters.num_workers = 1`
     `save_verified_cpsat_python`'s optional `seed` argument sets this
     environment variable for the replay re-run; a script that hardcodes the
     seed instead of reading this env var silently ignores the replay. The
     server cannot force a seed into arbitrary Python.
   - Solve: `status_code = solver.solve(model)`.
   - Emit exactly ONE JSON object as the LAST line of stdout:
     ```
     status_map = {{
         cp_model.OPTIMAL: "optimal",
         cp_model.FEASIBLE: "feasible",
         cp_model.INFEASIBLE: "infeasible",
         cp_model.UNKNOWN: "unknown",
     }}
     solution = {{}}
     if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
         solution = {{"var1": solver.value(var1), ...}}
     print(json.dumps({{
         "status": status_map.get(status_code, "error"),
         "objective": float(solver.objective_value) if model.has_objective() else None,
         "solution": solution,
         "best_objective_bound": (
             float(solver.best_objective_bound) if model.has_objective() else None
         ),
     }}))
     ```
     `best_objective_bound` (OR-Tools' `solver.best_objective_bound` — a
     PROPERTY, not a method) is a diagnostic bound, not a proven objective.
     Include it for every optimization model so a `status="unknown"` result
     still carries search-progress information even with no incumbent.
     CRITICAL for a PURE FEASIBILITY problem (no `model.minimize`/`maximize`
     call — a satisfaction-only CSP, e.g. a scheduling or CSP-only model with
     no cost to optimize): `model.has_objective()` is `False`, and BOTH
     `solver.objective_value` AND `solver.best_objective_bound` then return
     `0.0` WITHOUT raising — never omit the `if model.has_objective() else
     None` guard, or a feasibility problem's `unknown`/`error` result would
     misleadingly report a "bound" of `0.0` instead of `null`. There is no
     meaningful bound for a satisfaction-only problem; `null` is the correct
     value, not `0.0`.
   - For a long or optimization run that may hit `timeout_ms`, ALSO emit an
     intermediate JSON object of the SAME shape on each improved solution,
     from a `cp_model.CpSolverSolutionCallback`, e.g.:
     ```
     class _Best(cp_model.CpSolverSolutionCallback):
         def __init__(self, variables, has_objective):
             super().__init__()
             self._variables = variables
             self._has_objective = has_objective
         def on_solution_callback(self):
             print(json.dumps({{
                 "status": "feasible",
                 "objective": self.objective_value if self._has_objective else None,
                 "solution": {{name: self.value(v) for name, v in self._variables.items()}},
                 "best_objective_bound": (
                     self.best_objective_bound if self._has_objective else None
                 ),
             }}))
     solver.solve(model, _Best({{"var1": var1, ...}}, model.has_objective()))
     ```
     Pass `model.has_objective()` into the callback (not just `model`/`solver`
     module-level) so the SAME `0.0`-vs-`null` guard from the final block
     above applies here too — a feasibility problem's callback fires on every
     found solution during a long satisfaction search, not just optimization
     runs, so it needs the identical guard.
     The child runs unbuffered, so on a timeout the server recovers the last
     such block as the best-so-far. The final block (printed after `Solve`
     returns) remains the authoritative result on a clean run.
   - SAFETY: generate only CP-SAT modeling code — no network access, no
     file writes or deletes, no subprocess spawning — unless the user
     explicitly requested it. The server executes this code locally in a
     child process and does not sandbox it.

4. Call `run_cpsat_python` with the script as `source`. The server runs it
   locally in a child process (not remote, not sandboxed) and returns a
   `CpsatPythonResult` with `status`, `solution`, `objective`,
   `best_objective_bound` (diagnostic only — see step 3), `stdout`,
   `stderr`, `timed_out`, `truncated`, and `duration_ms`.

5. Present the result clearly:
   - Distinguish `optimal` (proven best) from `feasible` (valid but
     unproven optimal). Never describe a `feasible` result as optimal.
   - For `infeasible` or `error`, say so plainly; point at `stderr` on
     `error`. For `timeout`, the child process exceeded `timeout_ms`; if
     `solution` is populated it is the best found so far (unproven, treat as
     feasible-not-optimal), otherwise none was reached in time.
   - For `unknown` (no incumbent found), mention `best_objective_bound` when
     present — it shows the solver made bound progress even though no
     feasible solution was found; it is a diagnostic hint, not a solution.
   - Describe the solution in the user's own terms (task names, variable
     semantics), not as a raw JSON dump.
   - For a HARD instance where CP-SAT's own result quality is unclear, also
     consider the MiniZinc portfolio path (`solve_constraint_problem` prompt,
     `submit_portfolio_job`) for the same problem, potentially trying multiple
     formulations and solvers — neither backend dominates for every problem
     shape, and the server's structured results and checkers from both let
     you compare outcomes before committing to one.

6. For MULTIPLE explicit attempts — comparing model/source variants, or the
   same source under different cooperative configs — use
   `run_cpsat_python_experiment` instead of calling `run_cpsat_python`
   repeatedly yourself. YOU always write every attempt's complete `source`;
   the server never generates, diffs, or merges attempts — it only executes
   what you give it, verifies acceptance, and selects a winner.
   - Each attempt is `{{name, source, seed, config, timeout_ms}}`. `source`
     is a full, independent script (same SAFETY rule as step 3: model,
     solve, and stdout JSON only — no network access, file writes/deletes,
     or subprocess spawning unless the user explicitly requested it). `seed`
     and `config` are optional cooperative protocols: a script must opt in
     to read them, and a non-cooperating script simply ignores them.
   - To vary the SAME script by a cooperative config instead of pasting it
     multiple times, have the script read
     `os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")`, load that path as
     JSON, and apply whichever fields it defines, e.g.:
       `config_path = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")`
       `config = json.load(open(config_path)) if config_path else {{}}`
       `solver.parameters.num_workers = config.get("num_workers", 1)`
     The server only writes this JSON to a temp file and points the env var
     at it — it never sets OR-Tools parameters itself. An empty `config`
     (`{{}}`) behaves identically to omitting it.
   - If you set `max_parallel_attempts > 1`, keep each attempt's own
     `solver.parameters.num_workers` conservative: `max_parallel_attempts *
     num_workers` oversubscribing the machine's CPUs makes runs slower and
     less stable, not faster.
   - Present the winner plus the full attempt table (every attempt's status,
     objective, and whether it was accepted/rejected and why). A `timeout`
     winner is a best-so-far incumbent, not proven optimal, and is not yet
     savable — re-run just that attempt with a larger `timeout_ms` first.

7. Persist only if the user asks. Call `save_verified_cpsat_python` with
   the script as `source`, the original problem text as `problem`, and the
   user's chosen save directory as an explicit absolute `target_dir`. You
   ask the user for that path; the server opens no file dialog. The server
   re-runs the script to evaluate the save gate before writing anything.
   Replacing a previously saved directory needs `overwrite=true`.
   Optional `seed` is a single-run replay aid: the re-run replays that seed
   and the manifest records it. The save gates are UNCHANGED, so a
   `timeout` result still fails the reported gate regardless of `seed` —
   re-run it to optimal/feasible first. A saved seeded model reproduces by
   hand only when you set `OPENCONSTRAINT_MCP_CPSAT_SEED` to the recorded
   seed; the saved `solution.py` carries only its own seed fallback.
   If the script you are saving came from `run_cpsat_python_experiment` —
   the winner, or any other attempt you chose to save instead — also pass
   its `config` (that attempt's exact config, `{{}}`/omitted if it ran
   without one) and that tool's result as `experiment_result`, so the full
   attempt table is persisted alongside the saved script as
   `experiment-log.json` — a provenance SUMMARY (hashes and scalar outcomes
   per attempt), not an archive of every attempt's full config. The server
   still re-verifies independently and never trusts the attached result as
   proof; `experiment_result` must describe an ACCEPTED attempt matching
   THIS exact save (matching source, seed, and config) or the save is
   rejected before it re-runs anything.

   Save gate options (in order of strictness):
   a. Reported gate (always applied): `status` in `optimal`/`feasible` and
      non-empty `solution`. This is the minimum required to save and is the
      default when no `expectation` or `checker` is supplied.
   b. Expectation gate (optional): supply `expectation` with
      `objective_sense` ('maximize' or 'minimize') and a numeric
      `objective_threshold`. The server checks whether the script's reported
      objective meets this threshold. IMPORTANT: an expectation threshold is
      a quality gate or regression bound — it does NOT prove that the
      solution is globally optimal or that no better solution exists.
   c. Checker gate (optional): supply `checker` (a complete Python script as
      a source string) that independently validates the solution against
      problem-specific constraints. The checker script must:
      - Accept the payload JSON path as its FIRST positional argument
        (`sys.argv[1]`), e.g.: `payload = json.load(open(sys.argv[1]))`
      - The payload has keys: `problem` (str|null), `solution` (dict),
        `objective` (float|int|null), `solver_status` (str).
      - Print exactly ONE JSON object as its FINAL stdout line:
        `{{"status": "accepted"|"rejected"|"error", "errors": [...], "details": {{...}}}}`
      - `accepted` with an empty `errors` list is the only passing verdict.
      - SAFETY: generate only validation code — no network access, no
        file mutations, no subprocess spawning — unless the user explicitly
        requested it. The server executes this code locally and does not
        sandbox it.
   Write a checker when the user asks for independent validation, when
   the problem has structural constraints that cannot be inferred from
   the reported `status` alone, or when the result will be reused and
   higher confidence is valuable.

Boundaries:
- You write the CP-SAT Python script and any checker; openconstraint-mcp
  does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally in a child process — no remote backends, no
  uploads, no hidden network calls. The server wrapper makes no network
  calls; an LLM-generated script or checker that reaches the network is
  user-directed.
"""
