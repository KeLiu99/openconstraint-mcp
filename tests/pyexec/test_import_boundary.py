"""AST-scan import boundary test for the pyexec package.

No module under ``openconstraint_mcp/pyexec`` may import
``openconstraint_mcp.minizinc`` or ``openconstraint_mcp.runtime``.

Catches all import forms:
- ``from ..runtime import x``
- ``from .. import runtime``
- ``from openconstraint_mcp import runtime``
- ``import openconstraint_mcp.minizinc``
- ``as`` aliases (the underlying module path is checked, not the alias)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PYEXEC_DIR = Path(__file__).parent.parent.parent / "src" / "openconstraint_mcp" / "pyexec"

_FORBIDDEN_PREFIXES = (
    "openconstraint_mcp.minizinc",
    "openconstraint_mcp.runtime",
)


def _module_name_of(path: Path) -> str:
    """Return the dotted module name for a file under src/."""
    src = path.parents[len(path.parts) - path.parts.index("src") - 2]
    rel = path.relative_to(src / "openconstraint_mcp").with_suffix("")
    parts = ["openconstraint_mcp"] + list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_import(node: ast.Import | ast.ImportFrom, file_module: str) -> list[str]:
    """Return the list of absolute dotted module paths referenced by this node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]

    # ast.ImportFrom
    if node.level == 0:
        base = node.module or ""
        if node.names[0].name == "*":
            return [base]
        return [f"{base}.{alias.name}" if base else alias.name for alias in node.names] + (
            [base] if base else []
        )

    # Relative import: resolve package by going up `level` components.
    parts = file_module.split(".")
    # level=1 → same package; level=2 → parent package, etc.
    anchor_parts = parts[: -(node.level)]  # remove leaf + extra levels
    anchor = ".".join(anchor_parts)

    if node.module:
        base = f"{anchor}.{node.module}" if anchor else node.module
    else:
        base = anchor

    resolved = [base]
    for alias in node.names:
        resolved.append(f"{base}.{alias.name}" if base else alias.name)
    return resolved


def _is_forbidden(module_path: str) -> bool:
    return any(
        module_path == prefix or module_path.startswith(prefix + ".")
        for prefix in _FORBIDDEN_PREFIXES
    )


@pytest.mark.parametrize("pyfile", sorted(_PYEXEC_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_pyexec_file_has_no_minizinc_or_runtime_import(pyfile: Path) -> None:
    file_module = _module_name_of(pyfile)
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for resolved in _resolve_import(node, file_module):
            if _is_forbidden(resolved):
                violations.append(f"line {node.lineno}: imports {resolved!r}")

    assert not violations, (
        f"{pyfile.relative_to(_PYEXEC_DIR.parent.parent)} has forbidden imports:\n"
        + "\n".join(violations)
    )
