"""Prompt templates exposed by the MCP server.

Editorial copy for the server's MCP prompts lives here, separate from the
FastMCP wiring in ``server.py``. This is a leaf module: ``server`` imports it,
and it imports nothing internal.
"""

from __future__ import annotations

SOLVE_CONSTRAINT_PROBLEM_PROMPT = """\
You are the MCP client's reasoning model, helping the user solve a
constraint-programming or optimization problem with openconstraint-mcp.

openconstraint-mcp calls no LLM and embeds no agent framework. Its
deterministic local tools run MiniZinc on the user's machine: you draft the
model, the local managed runtime verifies and solves it.

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
same `CheckResult` / `SolveResult` shapes, so follow steps 4-7
substituting the TOOL, not just the argument: wherever step 4 says
`check_minizinc_model`, call `check_minizinc_files(model_path=<path>,
data_path=<path, when a data file exists>, solver=<chosen solver id>)`;
wherever steps 5-6 say `solve_minizinc_model`, call
`solve_minizinc_files(model_path=<path>, data_path=<path, when a data
file exists>, solver=<solver id>, timeout_ms=<milliseconds>)`. Never
pass `model_path` to the string tools.

Otherwise:

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective,
   tie-breakers), ask a few concise clarifying questions first. Do not
   silently invent values.

3. Draft a complete MiniZinc model: every variable and parameter
   declaration, every constraint, exactly one `solve` statement
   (`solve satisfy;`, `solve minimize <expr>;`, or `solve maximize <expr>;`),
   and an `output` block that prints the solution self-describingly.
   Default to the `cp-sat` solver unless the user says otherwise. For a
   specific number of distinct satisfaction solutions, pass `num_solutions`
   with `org.gecode.gecode` or `org.chuffed.chuffed` — the default `cp-sat`
   does not support it. For multiple optimal solutions, first solve the
   optimization to a proven optimum; then add a constraint fixing the
   objective expression to that value, switch to `solve satisfy;`, and
   enumerate with one of those supported solvers plus `num_solutions`.

4. Validate before solving: call `check_minizinc_model(model=<model text>,
   data=<dzn text, omitted when there is none>, solver=<chosen solver id>)`
   and branch on the returned `status`; never solve before a check has
   returned `"ok"`. The recommended loop is
   `draft -> check_minizinc_model -> repair -> solve_minizinc_model -> explain`.
   Pass the same `data` and `solver` to both the check and the solve so you
   validate the instance and configuration you solve.
   - `"ok"`: the model compiles; proceed to solving.
   - `"error"`: read the `stderr` diagnostics, repair, and re-check until
     `"ok"`.
   - `"timeout"`: validation itself — not the solve — timed out. Do not
     auto-solve. Explain this and let the user choose: simplify the model,
     raise `timeout_ms`, or solve anyway — the one exception to the
     `"ok"`-before-solve rule, taken only on the user's explicit choice.

5. Execute the model:
   - If `solve_minizinc_model` is listed among the tools you can call,
     invoke it as `solve_minizinc_model(model=<model text>, data=<same
     data>, solver=<chosen solver id>, timeout_ms=<milliseconds>)` — that
     exact name, no invented tools or extra arguments — and let the local
     managed runtime solve.
   - If it is not listed, do not fabricate a tool call, and do not tell
     the user to run a bare `minizinc` from their PATH — that bypasses the
     managed runtime and can pick up a different version. Instead walk them
     through the CLI:
       a. `openconstraint-mcp check-runtime` to confirm the managed runtime
          is installed and read its `minizinc` binary path.
       b. If the runtime is missing, either `openconstraint-mcp
          install-runtime` to download the managed bundle, or
          `openconstraint-mcp configure-runtime --runtime-dir <path>`
          (equivalently `OPENCONSTRAINT_MCP_RUNTIME_DIR=<path>`) to point at
          an existing install, then re-run `check-runtime`.
       c. Present the model as a code block and have them solve it by
          invoking that exact managed binary with the chosen solver flag,
          e.g. `--solver cp-sat`.

6. On a syntax, type, or solver error, revise and retry — but only through
   `solve_minizinc_model` when that tool is listed; never fabricate solver
   output. For a HARD problem — the solve returned `status` `unknown` or
   `timeout`, or `satisfied` on an optimization when the user needs a
   proven optimum, or several plausible formulation/solver choices remain —
   explore rather than settle for one run. Once the latest check has
   returned `"ok"`, race alternatives with a background portfolio via
   `submit_portfolio_job(models=[<model texts>], solvers=[<solver ids>],
   data=<same data>)`, polling `get_portfolio_job(job_id=<returned id>)`
   for the winner and the full per-attempt table, varying any of: model
   formulations (`models`, a list — e.g. a different variable encoding,
   redundant constraints, or symmetry-breaking constraints), `solvers`,
   seeds (`seed_count` or an
   explicit `seeds` list), and search controls (`free_search`, `parallel`,
   and each attempt's `per_attempt_timeout_ms` budget).
   For an especially hard instance, also consider the OR-Tools CP-SAT
   Python path (`cpsat_python_solution_workflow` prompt, `run_cpsat_python`) on the
   same problem — neither backend dominates every problem shape, and the
   structured results and checkers from both let you compare outcomes
   before committing to one. When the user instead wants SEVERAL candidate
   formulations raced against each other before committing to any one of
   them, use the `auto_tune_constraint_problem` prompt instead of ad hoc
   solo runs — it structures a three-tier smoke/tuning/full-instance race.

7. Present the result as a short, structured summary; do not dump the raw
   `SolveResult`. Lead with the result itself; do not narrate the prompt,
   workflow, or tool names you used unless the user explicitly asks for
   those implementation details. Read the fields rather than guessing, and
   always cover:
   - the `diagnostic` first when present: `diagnostic.category` is a stable
     enum (`infeasible`, `unbounded`, `timeout_no_incumbent`,
     `timeout_with_incumbent`, `checker_failed`, `syntax_or_compile_error`,
     `missing_data`, `type_error`, …) you branch on before reading raw
     `stdout`/`stderr`. It is `null` on a clean success. Treat `status` and
     `diagnostic.category` as the primary signals and stdout/stderr/transcripts
     as supporting evidence.
   - the `status`, in plain language: distinguish a proven-optimal solution
     (`optimal`) from a feasible-but-unproven one (`satisfied`), and both
     from `unsatisfiable`, `unbounded`, `unknown`, `error`, and `timeout`.
     Never describe a merely `satisfied` result as optimal. Judge "not
     proven optimal" from `status`, not `timed_out`: cleanly hitting
     MiniZinc's own `timeout_ms` returns `timed_out` false with a feasible
     `satisfied`/`unknown`, whereas `timed_out` true means the hard
     subprocess cap killed the run (`return_code` null, `status` `timeout`,
     and `stdout` may be truncated). A non-zero `return_code` with `error`
     means MiniZinc itself failed — read `stderr`.
   - the solution, only when the result carries one (`satisfied` /
     `optimal`; for `timeout` see the diagnostic branch below):
     show it as a block read verbatim from raw `stdout` — the `output`
     block text is authoritative, so do not restate the values yourself.
     Use the structured fields to organize that display, never to replace
     it: `solution` is the best/last solution as a variable-name -> value
     map (model variables only; the objective is reported separately),
     `solutions` is every solution in emission order (for an optimization,
     the improving sequence; its last entry equals `solution`), and
     `objective` is the best objective value (null for a pure-satisfaction
     problem). Build any item table or cross-solution comparison from
     `solution` / `solutions`, report the optimized value from `objective`,
     and keep the verbatim block itself from `stdout`. When the problem
     supplies item-like data (items with weights/values, tasks, shifts,
     etc.) and the solution selects among it, include a compact table
     rather than a prose-only list: for small item sets (roughly 20 rows or
     fewer), one row per item with the item index/name, relevant
     attributes, and the selected/count value; for larger sets, a compact
     table of selected items plus totals.
     An `unsatisfiable` or `error` result has no solution to show: say so
     plainly, and for `error` point at `stderr`. For `timeout`, branch on
     the diagnostic: `timeout_with_incumbent` means `solution` /
     `solutions` / `objective` hold the best found before the cap killed
     the run — present that as an unproven best-so-far, never as optimal;
     `timeout_no_incumbent` means there is no solution to show.
   - the complete model-visible `Statistics:` section is required whenever
     the `statistics` map is non-empty — do not omit it, summarize it, or
     replace it with only selected fields such as `solveTime` and
     `objectiveBound`. Copy the full section from the solve tool's text
     content into the user-facing answer. If the map is empty, say nothing
     of it; its keys vary by solver and are reported best-effort, not
     independently verified.

   Keep it tight: use each heading at most once, and by default add no
   speculative algorithm commentary (value-density ratios, greedy
   reasoning, alternative heuristics) unless the user asks for deeper
   analysis.

8. Persist only if the user asks to save the model: call
   `save_verified_minizinc_model(model=<final model text>, data=<final
   data>, checker=<checker, when one was used>, problem=<original problem
   text>, target_dir=<explicit absolute save directory>)` with the text
   exactly as last checked and solved. You ask the user for that path (or
   use your client's own file picker); the server opens no file dialog,
   and it re-verifies the artifacts through the managed runtime before
   writing anything.
   Replacing a previously saved directory needs `overwrite=true`, and only
   a directory written by a prior save can be replaced. If you explored via
   `submit_portfolio_job`, also pass that job's `PortfolioSolveResult` as
   `portfolio_result` so the winning race's full attempt table (every
   formulation/solver/seed tried, and why) is persisted alongside the saved
   model as `experiment-log.json`, and replay the winner's configuration in
   the save call itself: set `solver` and `random_seed` to the winning
   attempt's `solver` and `seed`, and `free_search` / `parallel` /
   `all_solutions` / `num_solutions` to the race's `solve_controls` values —
   the server rejects a save whose arguments do not match the winning
   attempt's configuration (`timeout_ms` is not compared). The server still
   re-verifies independently and never trusts the attached result as proof.

Boundaries:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp owns no LLM credentials and invokes no generative
  model.
- All solving runs locally through the managed MiniZinc runtime — no remote
  backends, no uploads, no hidden network calls.
"""

SOLVE_CPSAT_PYTHON_PROMPT = """\
You are the MCP client's reasoning model, helping the user solve a
constraint-programming or optimization problem using OR-Tools CP-SAT Python
through openconstraint-mcp.

openconstraint-mcp calls no LLM. Its deterministic local tools execute
Python scripts in a child process on the user's machine: you write the
CP-SAT script, `run_cpsat_python` runs it locally and returns a structured
result.

User problem:
{problem}

1. Analyze the problem: decision variables and their domains, hard
   constraints, and the objective (minimize / maximize, or "satisfy" for a
   pure feasibility problem).

2. If anything important is missing (sizes, bounds, the objective,
   tie-breakers), ask concise clarifying questions first. Do not silently
   invent values.

3. Write a complete, runnable OR-Tools CP-SAT Python script. For a SINGLE
   problem instance — the common case — hardcode the actual parameter values
   (e.g. the real player/group/week counts for a social golfer instance)
   directly in the script rather than a named "scenario" that needs a
   `config` to resolve. Reserve the cooperative `config` /
   `OPENCONSTRAINT_MCP_CPSAT_CONFIG` protocol (step 6) for EXPLICIT
   multi-attempt or configured experiments — it is not the default modeling
   style for a one-off save.
   - For a REPRODUCIBLE saved artifact, READ the seed from the environment
     (falling back to 42) and keep a single search worker, exactly as the
     example below does. `save_verified_cpsat_python`'s optional `seed`
     argument sets the `OPENCONSTRAINT_MCP_CPSAT_SEED` environment variable
     for the replay re-run; a script that hardcodes the seed instead
     silently ignores the replay — the server cannot force a seed into
     arbitrary Python.
   - Emit exactly ONE JSON object as the LAST line of stdout. Complete
     runnable example — replace the toy model with the real one and keep
     the emitted JSON contract exactly:
     ```
     import json
     import os

     from ortools.sat.python import cp_model

     model = cp_model.CpModel()
     x = model.new_int_var(0, 10, "x")
     y = model.new_int_var(0, 10, "y")
     model.add(x + y <= 12)
     model.maximize(x + 2 * y)

     solver = cp_model.CpSolver()
     solver.parameters.random_seed = int(
         os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42")
     )
     solver.parameters.num_workers = 1
     status_code = solver.solve(model)

     status_map = {{
         cp_model.OPTIMAL: "optimal",
         cp_model.FEASIBLE: "feasible",
         cp_model.INFEASIBLE: "infeasible",
         cp_model.UNKNOWN: "unknown",
     }}
     has_solution = status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
     solution = {{}}
     if has_solution:
         solution = {{"x": solver.value(x), "y": solver.value(y)}}
     bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
     print(json.dumps({{
         "status": status_map.get(status_code, "error"),
         "objective": (
             float(solver.objective_value)
             if model.has_objective() and has_solution
             else None
         ),
         "solution": solution,
         "best_objective_bound": (
             float(solver.best_objective_bound)
             if model.has_objective() and status_code in bound_states
             else None
         ),
     }}))
     ```
     `best_objective_bound` (OR-Tools' `solver.best_objective_bound` — a
     PROPERTY, not a method) is a diagnostic bound, not a proven objective.
     Include it for every optimization model so a `status="unknown"` result
     still carries search-progress information even with no incumbent.
     CRITICAL: neither property raises when it has nothing meaningful to
     report — it returns `0.0` or another arbitrary number instead.
     `solver.objective_value` is meaningless without an incumbent: for a
     PURE FEASIBILITY problem (no `model.minimize`/`maximize` call, so
     `model.has_objective()` is `False`), and for an optimization run that
     ends `infeasible` or `unknown` with no solution.
     `solver.best_objective_bound` is meaningless for a pure feasibility
     problem and for `infeasible`/`error`, but on `unknown` it is genuine
     search progress. Never drop the example's guards: emit `objective`
     only for a solution-bearing status (`optimal`/`feasible`), emit
     `best_objective_bound` only for `optimal`/`feasible`/`unknown`, and
     emit `null`, not a fabricated number, in every other case.
   - For a long or optimization run that may hit `timeout_ms`, ALSO emit an
     intermediate JSON object of the SAME shape on each improved solution,
     from a `cp_model.CpSolverSolutionCallback`. Replace the example's plain
     `status_code = solver.solve(model)` line with:
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

     status_code = solver.solve(model, _Best({{"x": x, "y": y}}, model.has_objective()))
     ```
     Pass `model.has_objective()` into the callback so the same
     `0.0`-vs-`null` guard applies there too — a feasibility problem's
     callback fires on every found solution, not just optimization runs.
     The child runs unbuffered, so on a timeout the server recovers the
     last such block as the best-so-far. The final block (printed after
     `solve` returns) remains the authoritative result on a clean run.
   - SAFETY: generate only CP-SAT modeling code — no network access, no
     file writes or deletes, no subprocess spawning — unless the user
     explicitly requested it. The server executes this code locally in a
     child process and does not sandbox it.

4. Call `run_cpsat_python(source=<complete script>,
   timeout_ms=<milliseconds>)`. The server runs it locally in a child
   process (not remote, not sandboxed) and returns a `CpsatPythonResult`
   with `status`, `solution`, `objective`, `best_objective_bound`
   (diagnostic only — see step 3), `stdout`, `stderr`, `return_code`,
   `timed_out`, `truncated`, `duration_ms`, and a structured `diagnostic`
   (`null` on a clean success).

5. Present the result clearly:
   - Read `diagnostic` first when present: `diagnostic.category` is a stable
     enum (`infeasible`, `timeout_no_incumbent`, `timeout_with_incumbent`,
     `output_truncated`, `child_process_error`, `checker_failed`, …) you
     branch on before reading raw `stdout`/`stderr`. Treat `status` and
     `diagnostic.category` as the primary signals and raw `stdout`/`stderr`
     as supporting evidence, never the primary status signal.
   - Distinguish `optimal` (proven best) from `feasible` (valid but
     unproven optimal). Never describe a `feasible` result as optimal.
   - For `infeasible` or `error`, say so plainly; point at `stderr` on
     `error`. For `timeout`, the child process exceeded `timeout_ms`; a
     populated `solution` is the best found so far (unproven, treat as
     feasible-not-optimal), otherwise none was reached in time.
   - For `unknown` (no incumbent found), mention `best_objective_bound`
     when present — it shows the solver made bound progress, but it is a
     diagnostic hint, not a solution.
   - Describe the solution in the user's own terms (task names, variable
     semantics), not as a raw JSON dump.
   - For a HARD instance — `status` is `unknown` or `timeout`, or
     `feasible` when the user needs a proven optimum — also consider the
     MiniZinc portfolio path (`minizinc_solution_workflow` prompt,
     `submit_portfolio_job`) on the same problem — neither backend
     dominates every problem shape, and the structured results and checkers
     from both let you compare outcomes before committing to one. When the
     user instead wants SEVERAL candidate formulations raced against each
     other before committing to any one of them, use the
     `auto_tune_constraint_problem` prompt instead of ad hoc solo runs — it
     structures a three-tier smoke/tuning/full-instance race.

6. For MULTIPLE explicit attempts — comparing model/source variants, or the
   same source under different cooperative configs — use
   `run_cpsat_python_experiment(attempts=[<attempt objects>],
   objective_sense=<"minimize" | "maximize"; omit for pure feasibility>)`
   instead of calling `run_cpsat_python` repeatedly yourself. YOU always
   write every attempt's complete `source`; the server never generates,
   diffs, or merges attempts — it only executes what you give it, verifies
   acceptance, and selects a winner.
   - Each attempt is `{{name, source, seed, config, timeout_ms}}`. `source`
     is a full, independent script (same SAFETY rule as step 3). `seed` and
     `config` are optional cooperative protocols: a script must opt in to
     read them, and a non-cooperating script simply ignores them.
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
     `solver.parameters.num_workers` conservative: oversubscribing the
     machine's CPUs makes runs slower and less stable, not faster.
   - Present the winner plus the full attempt table (every attempt's
     status, objective, and whether it was accepted/rejected and why). A
     `timeout` winner is a best-so-far incumbent, not proven optimal, and
     not yet savable — re-run just that attempt with a larger `timeout_ms`
     first.

7. Persist only if the user asks: call
   `save_verified_cpsat_python(source=<final script>, problem=<original
   problem text>, target_dir=<explicit absolute save directory>)`. You
   ask the user for that path; the server opens no file dialog, and it
   re-runs the script to evaluate the save gate before writing anything.
   Replacing a previously saved directory needs `overwrite=true`.
   Optional `seed` is a single-run replay aid: the re-run replays that seed
   and the manifest records it. The save gates are UNCHANGED, so a
   `timeout` result still fails the reported gate regardless of `seed` —
   re-run it to optimal/feasible first. A saved seeded model reproduces by
   hand only when you set `OPENCONSTRAINT_MCP_CPSAT_SEED` to the recorded
   seed; the saved `model.py` carries only its own seed fallback.
   If the script came from `run_cpsat_python_experiment` — the winner, or
   another attempt you chose to save — also pass that attempt's exact
   `config` (`{{}}`/omitted if it ran without one) and the tool's result as
   `experiment_result`, so the full attempt table is persisted alongside
   the saved script as `experiment-log.json` — a provenance SUMMARY (hashes
   and scalar outcomes per attempt), not an archive of every attempt's full
   config. The server still re-verifies independently and never trusts the
   attached result as proof; `experiment_result` must describe an ACCEPTED
   attempt matching THIS exact save (same source, seed, and config) or the
   save is rejected before it re-runs anything.

   Save gate options (in order of strictness):
   a. Reported gate (always applied): `status` in `optimal`/`feasible` and
      non-empty `solution`. This is the minimum required to save and the
      default when no `expectation` or `checker` is supplied.
   b. Expectation gate (optional): supply `expectation` with
      `objective_sense` ('maximize' or 'minimize') and a numeric
      `objective_threshold`. The server checks the script's reported
      objective against this threshold. It is a quality gate or regression
      bound — it does NOT prove the solution is globally optimal.
   c. Checker gate (optional): supply `checker` (a complete Python script
      as a source string) that independently validates the solution against
      problem-specific constraints. The checker script must:
      - Read the payload JSON path from its FIRST positional argument
        (`sys.argv[1]`), e.g. `payload = json.load(open(sys.argv[1]))`.
        The payload has keys `problem` (str|null), `solution` (dict),
        `objective` (float|int|null), and `solver_status` (str).
      - Print exactly ONE JSON object as its FINAL stdout line:
        `{{"status": "accepted"|"rejected"|"error", "errors": [...], "details": {{...}}}}`
        `accepted` with an empty `errors` list is the only passing verdict.
      - SAFETY: generate only validation code — no network access, no file
        mutations, no subprocess spawning — unless the user explicitly
        requested it. The server executes this code locally and does not
        sandbox it.
   Write a checker when the user asks for independent validation, when the
   problem has structural constraints the reported `status` alone cannot
   confirm, or when the result will be reused and higher confidence is
   valuable.

8. To replay a saved artifact later, read its
   `.openconstraint-model.json` manifest and call
   `run_cpsat_python_file(script_path=<saved model.py path>,
   seed=<manifest verification.replay_seed>, config=<parsed
   replay-config.json contents, when that sibling file exists>)` — no
   manual environment variables needed. `run_cpsat_python_file` has no
   checker parameter, so this only re-verifies at the `reported` level
   even for a `checked`-level save. For full checked replay, call
   `save_verified_cpsat_python` again
   with the saved source/checker/seed/config, a scratch `target_dir`, AND —
   whenever the manifest or saved directory has them — the original
   `problem` (read from `problem.txt` if a `problem` artifact is listed),
   `expectation` (rebuilt from `verification.expectation.objective_sense` /
   `objective_threshold` if present), and `timeout_ms` (from
   `verification.timeout_ms`). Omitting any of these changes what gets
   replayed: `problem` feeds the checker's payload directly, so a checker
   that reads it validates against different input; `expectation` is a gate
   that runs and can fail *before* the checker ever runs, so leaving it out
   silently skips the objective-threshold check; and `timeout_ms` is the
   solver's re-run budget (and, when `checker_timeout_ms` was not set
   explicitly, the checker's timeout too) — a different value can reach a
   different result under the same gates. Passing all of them reproduces
   every gate the original save ran, including the checker with the
   manifest's `verification.checker_timeout_ms` when present.

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

AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT = """\
You are the MCP client's reasoning model. Compare several MiniZinc and/or
OR-Tools CP-SAT formulations, then present one full-instance result.

Use this prompt only when the user asks to compare approaches, not
automatically after a hard single-backend run. This is client-side
orchestration: you draft and select candidates; openconstraint-mcp only
checks, runs, and verifies them locally.

User problem:
{problem}

Use three tiers: a tiny smoke instance rejects broken candidates, a separate
representative tuning instance selects one provisional candidate per backend,
and the full instance re-checks and solves each finalist. Only the
full-instance final run's result is ever presented to the user or used as
save-tool provenance.

1. Identify the decision variables, domains, constraints, and objective. Ask
   concise questions if required values, bounds, tie-breakers, or the objective
   are missing; do not invent them.

2. Look for an existing `.mzn`/`.dzn` pair or CP-SAT `model.py`. If found,
   review it (revise only with the user's agreement) and include it as ONE
   candidate formulation in the drafted set. Do not ignore it, and do not
   treat it as the only candidate.

3. Draft a small candidate set. Vary something that actually changes the
   SEARCH SPACE, not just cosmetic structure:
   - symmetry breaking; for interchangeable objects, draft one candidate WITH
     symmetry breaking and one WITHOUT;
   - implied/redundant constraints;
   - global vs. decomposed constraints such as `alldifferent`, `cumulative`,
     or `circuit` and their CP-SAT equivalents;
   - variable domain tightening.
   Do not draft candidates that differ only in variable naming, constraint
   ordering, or code style. Search STRATEGY is a second, complementary axis,
   distinct from search space size.

   Backend rules:
   - MiniZinc: fix ONE shared `.dzn` parameter interface (names, types, and
     shapes) across all candidates. Only data values scale between tiers; the
     parameter interface itself stays fixed. When an existing `.mzn` is a
     candidate, the shared interface is that model's interface: new candidates
     conform to it, and the existing `.mzn` text must never be rewritten to
     fit it. If the existing `.mzn` hardcodes instance data instead of reading
     it from a `.dzn`, it cannot scale through data values alone: ask the user
     before deriving a parameterized copy for multi-scale racing. Without
     permission, race it only at its existing scale and skip tiers it cannot
     reach. For search strategy, pair `restart_luby` or `restart_geometric`
     with multiple tuning-stage seeds.
     Only Gecode/Chuffed honor restart annotations; CP-SAT ignores them and
     runs its own restarts, so pair a restart-annotated candidate with a
     restart-aware solver in `solvers`, not with `org.cp-sat`.
   - CP-SAT: hardcode the smoke values first. It will be REWRITTEN, not reused
     verbatim, at the representative tuning and full-instance stages; the
     provisional candidate is an approach, not a fixed source string. Use an
     existing `model.py` as-is for smoke, but the original file is never
     overwritten in place by a stage rewrite; the only write to the original
     file's path remains the explicit final save step. For search strategy,
     `solver.parameters.num_workers` above 1 enables OR-Tools' portfolio,
     which already includes automatic LNS and restarts. Do not draft a custom
     fix-and-reoptimize LNS loop. Multiple workers trade reproducibility for
     search power, so rerun every tier. Generate only CP-SAT modeling code: no
     network access, no file writes or deletes, and no subprocesses unless the
     user explicitly requested it.

4. Create a tiny smoke instance and use it ONLY to reject structurally broken
   candidates: `inspect_minizinc_model` then `check_minizinc_model` for each
   MiniZinc candidate, and one short `run_cpsat_python` per CP-SAT candidate.
   Call shapes: `inspect_minizinc_model(model=<candidate>, data=<smoke
   dzn>, solver=<a solver it will race with>)`, then
   `check_minizinc_model(model=<candidate>, data=<smoke dzn>, solver=<a
   solver it will race with>)`;
   `run_cpsat_python(source=<smoke script>, timeout_ms=<short budget>)`.
   For a candidate that already exists on disk (step 2), use the path-based
   tools instead — `inspect_minizinc_files`/`check_minizinc_files` with
   `model_path`/`data_path`, and `run_cpsat_python_file(script_path=<the
   existing model.py>, timeout_ms=<short budget>)` — they run from the
   file's own directory, so relative includes and sibling data resolve.
   This step never ranks or selects a winner among the candidates that pass.

5. Call `list_available_solvers` before any MiniZinc portfolio work.

6. Create a SEPARATE, larger representative tuning instance that exercises
   the problem's structure. Never rank or select using the smoke instance's
   results. Reuse MiniZinc's fixed `.dzn` interface with larger values. Each
   CP-SAT candidate is REWRITTEN with the representative tuning instance's
   values hardcoded.

7. Choose winners WITHIN a backend only; never merge candidates from both
   backends into one race. Draft a checker whenever more than one candidate is
   being compared, not only across backends: a checker is what stops an
   incorrect formulation from winning the tuning-stage race. For a
   cross-backend comparison, draft TWO backend-specific checkers that enforce
   the same problem constraints. They are NOT interchangeable source:
   MiniZinc uses inline MiniZinc solution-checker source; CP-SAT uses a Python
   script that reads the solution as JSON from `sys.argv[1]`. Compare each
   backend's final, checker-validated result across backends only when both
   represent the SAME objective and objective sense. When the objectives or
   senses don't match, ask the user which backend/result to keep instead of
   picking one yourself. Checkers remain optional for a single-backend,
   single-candidate run.

8. Select the PROVISIONAL MiniZinc candidate by submitting ONE
   `submit_portfolio_job` call PER smoke-surviving MiniZinc candidate (one
   model, with any solver/seed variants). Call shape:
   `submit_portfolio_job(models=[<one candidate>], solvers=[<solver ids>],
   data=<tuning dzn>, checker=<the MiniZinc checker>, seeds=[<tuning
   seeds>], per_attempt_timeout_ms=<budget>)`. Attach the checker when
   comparing candidates. Compare decisive, checker-accepted results
   across jobs: for an
   OPTIMIZATION problem, rank by best `objective`, then elapsed time; for a
   pure feasibility (`solve satisfy;`) problem there is no `objective`, so
   rank by `status` instead (`satisfied` outranks `unsatisfiable`), then elapsed
   time. NEVER race multiple candidate formulations inside one
   `submit_portfolio_job` call: its `first-decisive-result` winner treats
   `unsatisfiable` and `unbounded` as decisive, while the checker verdict is
   observational. A buggy formulation could otherwise win. Never trust one
   portfolio's `winner` across formulations.

9. Select the PROVISIONAL CP-SAT candidate with ONE
   `run_cpsat_python_experiment` call across the smoke-surviving CP-SAT
   candidates. Call shape:
   `run_cpsat_python_experiment(attempts=[<attempt objects>],
   objective_sense=<"minimize" | "maximize"; omit for pure feasibility>,
   checker=<the CP-SAT checker>, problem=<original problem text>)`, where
   each attempt is `{{name, source, seed, config, timeout_ms}}` with a
   complete, independent `source`. Attach the checker when comparing
   candidates. The tool accepts only attempts with a present solution
   and, when supplied, an `"accepted"` checker result, so it is NOT
   required to split into per-candidate calls.

10. Do not present a provisional candidate as the answer, and do not use its
    result as save-tool provenance.

11. Re-check each provisional formulation on the FULL instance:
    - MiniZinc: use a BOUNDED `solve_minizinc_model`/`solve_minizinc_files` call
      with full `data`, a short `timeout_ms`, and the checker —
      `solve_minizinc_model(model=<finalist>, data=<full data>, checker=<the
      checker>, solver=<winning solver>, timeout_ms=<short budget>)`. Never
      `check_minizinc_model`/`check_minizinc_files`: "`ok` means it compiles,
      not that it is satisfiable."
    - CP-SAT: REWRITE the provisional approach with the full instance's values
      hardcoded, then use `submit_cpsat_python_job` with the checker and poll
      `get_cpsat_python_job` until terminal. Call shape:
      `submit_cpsat_python_job(source=<full-instance script>, checker=<the
      CP-SAT checker>, problem=<original problem text>,
      timeout_ms=<budget>)`. `run_cpsat_python` has no `checker`
      parameter at all. Keep this exact full-instance `source` for the final
      job and any save.
    - Stop on MiniZinc's `unsatisfiable`/`error` or CP-SAT's
      `infeasible`/`error`; CP-SAT's status vocabulary has no `unsatisfiable`
      value. STOP and report the failure to the user instead of proceeding to
      the final solve.
    - A `timeout`/`unknown` re-check with NO incumbent solution is
      INCONCLUSIVE: proceed to the final solve, but flag that the pre-check did
      not confirm feasibility. MiniZinc returns a `checker.status` of
      `no_solution`; CP-SAT sets `checker_skipped_reason` instead of running
      `checker`. Do not apply the checker gate below to it.
    This is a pass/fail gate, not the result presented to the user.

12. Apply this checker gate to the re-check and final result whenever a
    solution exists and a checker was attached: the checker outcome must be a
    CLEAN pass to count as verified. MiniZinc requires a `checker.status` of
    exactly `"completed"` (a portfolio attempt reports the same verdict as
    `checker_status`); CP-SAT requires a `checker.status` of exactly
    `"accepted"`. Anything short of `checker.status == "completed"` /
    `checker.status == "accepted"` — a `violation`/`rejected` verdict, or a
    checker `error`/`timeout` on a real solution — means correctness was NOT
    confirmed: STOP. Once a solution exists, this gate has no inconclusive
    middle ground.

13. Submit the full-instance final solve:
    - MiniZinc: if `portfolio_result` provenance will be saved, use
      `submit_portfolio_job(models=[<the finalist model>],
      solvers=[<winning solver>], data=<FULL-instance dzn>, checker=<the
      checker>, seeds=[<winning seed>], per_attempt_timeout_ms=<final
      budget>)`, even for one model/solver — not step 8's shape: the save's
      data-hash consistency check rejects a `portfolio_result` produced
      with tuning data; otherwise use `submit_solve_job(model=<finalist>,
      data=<full data>, checker=<the checker>, solver=<winning solver>,
      random_seed=<winning seed>, timeout_ms=<final budget>)` — its
      `SolveResult` carries no `portfolio_result` field. When the winning
      attempt's `seed` is null (a race run without explicit `seeds`),
      omit `seeds` / `random_seed` from these calls entirely — `seeds`
      accepts only integers, and an unseeded winner has no seed to
      replay.
    - CP-SAT: use the SYNCHRONOUS `run_cpsat_python_experiment` (step 9's
      call shape) if `experiment_result` provenance will be saved. If that
      cannot fit a synchronous call, use `submit_cpsat_python_job` (step
      11's call shape) and save without `experiment_result`; there is no
      background experiment tool.
    Pass the relevant checker to the finalist call.

14. Poll the matching tool: `submit_portfolio_job` polls with
    `get_portfolio_job`; `submit_solve_job` polls with `get_solve_job`;
    `submit_cpsat_python_job` polls with `get_cpsat_python_job`. Each getter
    takes `job_id=<the id returned by its submit call>`. Read a synchronous
    experiment directly. Only this terminal result is presented.

    Before presenting a solution, apply step 12. A portfolio winner's
    `checker_status` is OBSERVATIONAL; `submit_solve_job`'s
    `SolveResult.checker` and `submit_cpsat_python_job`'s `checker` are also
    observational, so those tools may return an invalid solution. If the gate
    fails, STOP and report the violation to the user instead of presenting the
    result. A synchronous `run_cpsat_python_experiment` already filters
    rejected attempts, so this check is automatically satisfied whenever that
    path was used.

    A terminal `timeout`/`unknown` without an incumbent has nothing for the
    checker to check. MiniZinc reports `checker.status` of `no_solution`;
    CP-SAT sets `checker_skipped_reason` instead of `checker`. Present that
    result (flagged as unproven). Otherwise lead with the actual solve result
    and explain it in the user's terms: read `diagnostic.category` before raw
    `stdout`/`stderr` when a diagnostic exists (the raw streams are supporting
    evidence, never the primary status signal), describe `status` in the
    presenting backend's own vocabulary, never describe a result whose status
    is not `optimal` as optimal, and present a solution only when the status
    actually carries one. Include the complete `Statistics:` section whenever
    a MiniZinc result's `statistics` map is non-empty; CP-SAT results have no
    statistics map, so report none. Do not narrate the workflow or tool names
    you used unless the user asks for those implementation details.

15. Save only when the user asks — `save_verified_minizinc_model` for a
    MiniZinc finalist, `save_verified_cpsat_python` for a CP-SAT finalist —
    using an explicit absolute `target_dir` and the full-instance final
    run's result as provenance — never a smoke or representative-tuning
    result.
    `portfolio_result`/`experiment_result` are PROVENANCE ONLY; the save call
    hash-verifies provenance against the exact artifact and re-verifies it. It
    reaches checked verification only when the SAME `checker` you attached to
    the finalist run is passed directly to the save call itself.
    Dropping `checker` from the save call silently saves at a weaker level.
    - MiniZinc: pass the exact model/data/solver/seed (and, with
      `portfolio_result`, the race's
      `free_search`/`parallel`/`all_solutions`/`num_solutions` — the server
      rejects a save that does not replay the winning configuration), the
      SAME `checker` (when one was drafted for the finalist run), and the
      original problem text as
      `problem`; plus `portfolio_result` ONLY when the final run used
      `submit_portfolio_job`. A final run made through `submit_solve_job` has no
      `portfolio_result` to pass.
    - CP-SAT: pass the exact source/seed/config, the SAME `checker` (when one
      was drafted for the finalist run), and the original problem text as
      `problem`; plus `experiment_result` ONLY when the final run used the
      synchronous `run_cpsat_python_experiment`. A final run made through
      `submit_cpsat_python_job` has no `experiment_result` to pass.

Boundaries: openconstraint-mcp calls no LLM, runs no agent loop, and makes no
hidden network calls. Solving stays local; CP-SAT children are unsandboxed, so
generate no network or file-mutating code unless the user explicitly asks.
"""
