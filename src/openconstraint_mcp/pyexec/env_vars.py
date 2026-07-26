"""CP-SAT child-process protocol environment variable names.

A dependency-light leaf (stdlib only, no internal imports) shared by every
pyexec module that has to name these variables. ``core`` *sets* them on the
model child; ``checker`` *clears* them on the checker child. Keeping the
literals in one place means renaming a protocol variable can't leave one side
setting a name the other no longer strips.
"""

from __future__ import annotations

# Environment variable the seeded save replay sets for the child, carrying the
# replay CP-SAT random seed. The client-generated script must read it and assign
# ``solver.parameters.random_seed``; the server cannot force a seed into arbitrary
# Python.
CPSAT_SEED_ENV_VAR: str = "OPENCONSTRAINT_MCP_CPSAT_SEED"

# Environment variable an experiment attempt (or a config-carrying save replay)
# sets for the child, carrying the path to a temporary JSON config file. A
# cooperating script reads it and applies whichever fields it understands; the
# server never sets OR-Tools parameters itself — see CpsatPythonExperimentAttempt.
CPSAT_CONFIG_ENV_VAR: str = "OPENCONSTRAINT_MCP_CPSAT_CONFIG"
