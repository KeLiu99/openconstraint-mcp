"""Prompt templates exposed by the MCP server.

Editorial copy for the server's MCP prompts lives here, separate from the
FastMCP wiring in ``server.py``. This is a leaf module: ``server`` imports it
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

7. Once you have a result, explain it to the user in plain language: what
   the decision variables ended up as, whether the solution is optimal or
   only feasible, and any caveats worth noting.

Boundary reminders:
- You draft the MiniZinc model; openconstraint-mcp does not.
- openconstraint-mcp does not own LLM credentials and does not invoke a
  generative model.
- All solving runs locally on the user's machine through the managed
  MiniZinc runtime — no remote backends, no uploads, no hidden network
  calls.
"""
