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

# Run pytest with arbitrary args, e.g. `just pytest tests/test_cli.py::test_help_lists_all_commands -v`.
pytest *args:
    uv run pytest {{args}}

# Run real-runtime integration tests.
integration:
    uv run pytest -m integration -v

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

# Stage explicit files, commit, and push the current branch.
# Usage: just push "commit message" path/one path/two ...
# Safer default — only the listed files end up staged.
push msg +files:
    git add {{files}}
    git commit -m {{quote(msg)}}
    git push -u origin HEAD

# Stage *all* changes (incl. untracked), commit, and push the current branch.
# Usage: just push-all "commit message"
# Convenience for fully-trusted working trees — risks staging .env/secrets.
push-all msg:
    git add -A
    git commit -m {{quote(msg)}}
    git push -u origin HEAD

# Remove caches and build artefacts.
clean:
    rm -rf .pytest_cache .ruff_cache .mypy_cache build dist .coverage
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
