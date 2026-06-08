from __future__ import annotations

import json
from typing import Any

# Shared `--json-stream` test fixtures for the MiniZinc solve/checker parsers and
# the MCP tools that wrap them. The pinned managed runtime (MiniZinc 2.9.7) emits
# one JSON object per line; these builders serialize the exact shapes the parsers
# consume, so the captured format lives in one place instead of drifting across
# every test module. The parsers read only `output.default` and `output.json`
# (never `sections`/`raw`), so the richer keys here document the real transcript
# without affecting any parse result. Names are public (no leading underscore):
# this is a shared support module, so its exports are its API — same convention as
# the fixtures in `tests/conftest.py`.


class FakeCompletedProcess:
    """Stand-in for ``subprocess.CompletedProcess`` with just the attributes the
    MiniZinc runner reads: ``stdout``, ``stderr``, ``returncode``."""

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def stream(*objects: dict[str, Any]) -> str:
    """Serialize objects as a ``--json-stream`` transcript: one JSON object per line."""
    return "".join(json.dumps(obj) + "\n" for obj in objects)


def solution_obj(default: str, values: dict[str, Any]) -> dict[str, Any]:
    """A ``{"type":"solution"}`` object carrying both the human ``default`` text and
    the ``json`` variable map — the shape a model with an explicit ``output`` item emits."""
    return {
        "type": "solution",
        "output": {"default": default, "raw": default, "json": values},
        "sections": ["default", "raw", "json"],
    }


def solution_obj_json_only(values: dict[str, Any]) -> dict[str, Any]:
    """A solution object from a model with no explicit ``output`` item: under
    ``--output-mode json`` only the ``json`` section is present, so the human stdout
    has to be synthesized from the variable map."""
    return {"type": "solution", "output": {"json": values}, "sections": ["json"]}


VIOLATION_DIAGNOSTIC = "model inconsistency detected: expression evaluated to false"


def checker_pass(default_text: str) -> dict[str, Any]:
    """A clean ``--solution-checker`` verdict: the author CORRECT/INCORRECT text is
    surfaced verbatim from the checker's top-level ``output.default`` (the pinned
    runtime hoists it there). The documented nested-only shape is exercised
    separately by the checker parser's own tests."""
    return {
        "type": "checker",
        "messages": [{"type": "solution", "output": {"default": default_text}}],
        "output": {"default": default_text},
    }


def checker_violation() -> dict[str, Any]:
    """A constraint-style rejection: a nested ``status: UNSATISFIABLE`` (the one
    machine-readable "invalid solution" signal) plus a warning diagnostic, and no
    top-level rendered output."""
    return {
        "type": "checker",
        "messages": [
            {"type": "warning", "message": VIOLATION_DIAGNOSTIC},
            {"type": "status", "status": "UNSATISFIABLE"},
        ],
    }
