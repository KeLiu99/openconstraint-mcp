"""AST-scan test: ``@(async)contextmanager`` functions annotate ``(Async)Generator``.

typeshed marks the ``(Async)Iterator``-taking overload of
``contextlib.contextmanager``/``asynccontextmanager`` ``@deprecated``:
"Annotating the return type as ``-> Iterator[Foo]`` with ``@contextmanager`` is
deprecated. Use ``-> Generator[Foo]`` instead." Both overloads yield the same
``_GeneratorContextManager[T]``, so this is an annotation-style contract, not a
type-precision fix.

``just typecheck`` cannot be relied on to catch a regression here. mypy reports
``@deprecated`` only under the ``deprecated`` error code (enabled for this
project in ``pyproject.toml``), and the pinned mypy's bundled typeshed predates
the deprecation — its ``contextmanager`` is a single non-overloaded
``Callable[_P, Iterator[_T_co]]``, which accepts both forms without complaint.
Until that stub upgrade lands, this scan is the only guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent / "src" / "openconstraint_mcp"
_SRC_FILES = sorted(_SRC_DIR.rglob("*.py"))

_DECORATORS = {"contextmanager", "asynccontextmanager"}
_DEPRECATED_RETURNS = {"Iterator", "AsyncIterator"}


def _base_name(node: ast.expr) -> str | None:
    """Return the rightmost name of ``node``, unwrapping a subscript first.

    ``Iterator[None]`` → ``"Iterator"``; ``contextlib.contextmanager`` →
    ``"contextmanager"``; anything else (a call, a string annotation) → ``None``.
    """
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_scan_found_source_files() -> None:
    """Guard the guard: an empty parametrize list *skips*, leaving ``just check`` green.

    Without this, a layout change that breaks ``_SRC_DIR`` would silently retire
    the scan below instead of failing.
    """
    assert _SRC_DIR / "server.py" in _SRC_FILES


@pytest.mark.parametrize("pyfile", _SRC_FILES, ids=lambda p: str(p.relative_to(_SRC_DIR)))
def test_contextmanager_returns_generator_not_iterator(pyfile: Path) -> None:
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_base_name(dec) in _DECORATORS for dec in node.decorator_list):
            continue
        returns = _base_name(node.returns) if node.returns is not None else None
        if returns in _DEPRECATED_RETURNS:
            violations.append(f"line {node.lineno}: {node.name} returns {returns}")

    assert not violations, (
        f"{pyfile.relative_to(_SRC_DIR.parent.parent)} annotates a context manager with a "
        "deprecated (Async)Iterator return; use (Async)Generator:\n" + "\n".join(violations)
    )
