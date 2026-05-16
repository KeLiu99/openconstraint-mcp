# justfile for openconstraint-mcp
# All Python work goes through `uv` — never raw `python` or `pip`.

# Default: list available recipes.
default: list

# Show all recipes.
list:
    @just --list

# Sync dependencies, including dev group.
sync:
    uv sync --all-groups

# Run the MCP server over stdio.
run:
    uv run openconstraint-mcp stdio

# Run the CLI with arbitrary args, e.g. `just cli check-runtime`.
cli *args:
    uv run openconstraint-mcp {{args}}

# Run the test suite (exit 5 "no tests collected" tolerated until v0 skeleton lands).
test:
    @uv run pytest -ra || [ "$?" = "5" ]

# Lint the source tree with ruff.
lint:
    uv run ruff check .

# Auto-format with ruff (writes changes in-place).
format:
    uv run ruff format .

# Type-check the package source.
typecheck:
    uv run mypy src

# Full local gate: lint + typecheck + test.
check: lint typecheck test

# Remove caches and build artefacts.
clean:
    rm -rf .pytest_cache .ruff_cache .mypy_cache build dist .coverage
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
