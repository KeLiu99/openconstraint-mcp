"""Prompt templates exposed by the MCP server.

Editorial copy for the server's MCP prompts lives here, separate from the
FastMCP wiring in ``server.py``. This is a leaf module: ``server`` imports it,
and it imports nothing internal.
"""

from __future__ import annotations

SOLVE_CONSTRAINT_PROBLEM_PROMPT = """\
You are the MCP client's reasoning model helping the user solve a
constraint-programming or optimization problem using openconstraint-mcp.

openconstraint-mcp itself does not call any LLM and does not embed agent
frameworks. It only exposes MCP prompts and deterministic local tools that
run MiniZinc on the user's machine. The division of labor is: you draft the
model, the local managed MiniZinc runtime verifies and solves it.

User problem:
{problem}

If the user already has the model on disk as MiniZinc files — a `.mzn`
model plus an optional `.dzn` data file — you do not need to draft one.
Skip the drafting in steps 1-3 (review the existing files instead, and
revise them only with the user's agreement) and run the same
validate -> solve -> present loop through the path-based tools:
`check_minizinc_files` first, then `solve_minizinc_files`, passing
`model_path` (and `data_path` when a data file exists) rather than pasting
the file contents into the string tools — the path-based tools run from the
model's own directory, so a relative `include` resolves. Pass the same
`data_path` to both calls. They return the same `CheckResult` /
`SolveResult` shapes as the string tools, so steps 4-7 below apply
unchanged with `model_path` / `data_path` in place of `model` / `data`.

Do the following:

1. Analyze the problem. Identify:
   - decision variables and their domains,
   - hard constraints,
   - any objective (minimize / maximize), or "satisfy" if it is a pure
     feasibility problem.

2. If anything important is missing (sizes, bounds, the objective, tie-
   breakers), ask the user at most a few concise clarifying questions before
   drafting anything. Do not silently invent values.

3. When you have enough information, draft a complete MiniZinc model. The
   model must include:
   - every variable and parameter declaration,
   - every constraint,
   - exactly one `solve` statement (`solve satisfy;`,
     `solve minimize <expr>;`, or `solve maximize <expr>;`),
   - an `output` block that prints the solution in a self-describing form.

   Prefer `cp-sat` as the default solver unless the user has specified
   otherwise.

4. Validate the model before solving. If the tool `check_minizinc_model`
   is available to you, call it on the drafted model first and branch on
   the returned `status`. Never call `solve_minizinc_model` ahead of a
   clean check. The recommended loop is:
   `draft -> check_minizinc_model -> repair -> solve_minizinc_model -> explain`.
   When the drafted model relies on inline data, pass the same `data` to both
   the check and the solve call so you validate and solve the same instance.
   - `"ok"`: the model compiles. Proceed to solving. Do not solve until
     the check returns `"ok"`.
   - `"error"`: read the `stderr` diagnostics, repair the model, and
     re-run `check_minizinc_model`. Loop until it returns `"ok"`; do not
     solve while errors remain.
   - `"timeout"`: validation itself — not the solve — timed out. Do not
     automatically solve. Explain this to the user and ask how they want
     to proceed: simplify the model, raise `timeout_ms`, or try solving
     anyway. Continue only per the user's choice.

5. Execute the model:
   - If the tool `solve_minizinc_model` is available to you, call it with
     the drafted MiniZinc model and let the local managed MiniZinc runtime
     do the solving. Use exactly that tool name; do not invent a different
     one or invent additional arguments.
   - If `solve_minizinc_model` is not available to you yet, do not
     fabricate a tool call, and do not tell the user to run a bare
     `minizinc` from their PATH — that bypasses the managed runtime and
     can pick up a different MiniZinc version with different solvers.
     Instead, walk the user through the openconstraint-mcp CLI:
       a. Have them run `openconstraint-mcp check-runtime` to confirm
          the managed runtime is installed and to read the exact path
          of its `minizinc` binary.
       b. If `check-runtime` reports the runtime as missing, have them
          either run `openconstraint-mcp install-runtime` to download
          the managed bundle, or run
          `openconstraint-mcp configure-runtime --runtime-dir <path>`
          (equivalently, set `OPENCONSTRAINT_MCP_RUNTIME_DIR=<path>`)
          to point at an existing MiniZinc install, then re-run
          `check-runtime`.
       c. Present the complete MiniZinc model as a code block and have
          them solve it by invoking that exact managed `minizinc`
          binary (the path printed by `check-runtime`) with the chosen
          solver flag, e.g. `--solver cp-sat`.

6. If MiniZinc reports a syntax, type, or solver error, revise the model
   and retry — but only retry through `solve_minizinc_model` when that
   tool is actually available. Never fabricate solver output.

7. Once you have a result, present it to the user as a short, structured
   summary rather than dumping the raw `SolveResult`. Lead with the result
   itself; do not narrate the internal prompt, workflow, or tool names you
   used unless the user explicitly asks for those implementation details.
   Read its fields instead of guessing, and always cover these three things:
   - the solve `status`, in plain language: distinguish a proven-optimal
     solution (`optimal`) from a feasible-but-not-proven-optimal one
     (`satisfied`), and from `unsatisfiable`, `unbounded`, `unknown`,
     `error`, and `timeout`. Do not describe a merely `satisfied` result
     as optimal. Judge "not proven optimal" from `status`, not from
     `timed_out`: hitting MiniZinc's own `timeout_ms` budget returns
     cleanly (`timed_out` false) with a feasible-but-time-limited
     `satisfied`/`unknown` result, whereas `timed_out` true means the run
     blew past the hard subprocess cap and was killed before MiniZinc could
     stop itself (`return_code` is then null, `status` is `timeout`, and the
     captured `stdout` may be truncated mid-line, so read it cautiously). A
     non-zero `return_code` with `status` `error` means MiniZinc itself
     failed, so read `stderr`.
   - the solution itself, but only when the `status` carries one
     (`satisfied` / `optimal`): show it as a clear block read verbatim from
     raw `stdout` — the model's `output` block text is authoritative, so
     quote it rather than inferring or restating the values yourself. When
     the user problem supplies item-like data (items with weights/values,
     tasks, shifts, etc.) and the solution selects among it, add a concise
     selected-item table or list of the chosen elements and their totals.
     An `unsatisfiable`, `error`, or `timeout` result has no solution to
     show: say so plainly, and for `error` point the user at `stderr`.
   - a brief `statistics` summary is required whenever the `statistics`
     map is non-empty — do not omit it. Surface a few `%%%mzn-stat:`
     figures, preferring `objective`, `objectiveBound`, `nSolutions`,
     `failures`, `propagations`, and `solveTime` when present, as reported
     best-effort values. The map may be empty (then say nothing of it), its
     keys vary by solver and version, and it comes from an unauthenticated
     stream — present them as reported figures, not as independently
     verified or guaranteed solver-originated facts.

   Keep the presentation tight:
   - Use each section heading at most once; do not repeat a heading such as
     "Solver statistics".
   - By default, keep the explanation focused on verifying the result. Do
     not add speculative algorithm commentary — value-density ratios, greedy
     reasoning, alternative heuristics — unless the user asks for deeper
     analysis.

Boundary reminders:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp does not own LLM credentials and does not invoke a
  generative model.
- All solving runs locally on the user's machine through the managed
  MiniZinc runtime — no remote backends, no uploads, no hidden network
  calls.
"""
