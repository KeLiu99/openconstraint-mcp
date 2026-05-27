from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .minizinc import MiniZincExecutionError, list_solvers
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas import RuntimeStatus, SolverList

_SOLVE_CONSTRAINT_PROBLEM_PROMPT = """\
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

4. Execute the model:
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

5. If MiniZinc reports a syntax, type, or solver error, revise the model
   and retry — but only retry through `solve_minizinc_model` when that
   tool is actually available. Never fabricate solver output.

6. Once you have a result, explain it to the user in plain language: what
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


def create_server() -> FastMCP:
    mcp: FastMCP[Any] = FastMCP("openconstraint-mcp")

    @mcp.tool(description="Report whether the managed MiniZinc runtime is installed.")
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description="List solvers available in the managed MiniZinc runtime.")
    def list_available_solvers() -> SolverList:
        try:
            return list_solvers()
        except (RuntimeMissingError, MiniZincExecutionError) as exc:
            # v0: surface the error message as a plain MCP error so the
            # client sees something actionable. Future versions should return a
            # structured error envelope (e.g. {"code": "runtime_missing",
            # "hint": "run install-runtime"}) so MCP clients can branch on it
            # programmatically rather than parsing the message string.
            raise RuntimeError(str(exc)) from exc

    @mcp.prompt(
        name="solve_constraint_problem",
        description=(
            "Guide the MCP client's LLM through translating a natural-language "
            "constraint or optimization problem into MiniZinc and running it "
            "through the local managed runtime (via solve_minizinc_model when "
            "available, otherwise by walking the user through the "
            "openconstraint-mcp CLI to set up and invoke the managed runtime "
            "manually — never via a bare PATH-based minizinc)."
        ),
    )
    def solve_constraint_problem(problem: str) -> str:
        return _SOLVE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    return mcp


def run_stdio() -> None:
    create_server().run(transport="stdio")
