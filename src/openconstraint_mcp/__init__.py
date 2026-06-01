from __future__ import annotations

from .cli import app

__all__ = ["app", "main"]


def main() -> None:
    app()
