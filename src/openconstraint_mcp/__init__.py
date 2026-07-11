from __future__ import annotations

__all__ = ["main"]


def main() -> None:
    from .cli import app

    app()
