from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import PlatformDirs

from .schemas import RuntimeStatus

_APP_NAME = "openconstraint-mcp"
_ENV_RUNTIME_DIR = "OPENCONSTRAINT_MCP_RUNTIME_DIR"


class RuntimeMissingError(RuntimeError):
    """Raised when the managed MiniZinc runtime is not available."""


def get_runtime_dir() -> Path:
    override = os.environ.get(_ENV_RUNTIME_DIR)
    if override:
        return Path(override)
    dirs = PlatformDirs(_APP_NAME, _APP_NAME)
    return Path(dirs.user_data_dir) / "minizinc"


def get_minizinc_binary() -> Path:
    binary_name = "minizinc.exe" if sys.platform == "win32" else "minizinc"
    return get_runtime_dir() / "bin" / binary_name


def is_runtime_installed() -> bool:
    binary = get_minizinc_binary()
    if not binary.is_file():
        return False
    if sys.platform == "win32":
        return True
    return os.access(binary, os.X_OK)


def get_runtime_status() -> RuntimeStatus:
    binary = get_minizinc_binary()
    installed = is_runtime_installed()
    return RuntimeStatus(
        installed=installed,
        runtime_dir=str(get_runtime_dir()),
        minizinc_binary=str(binary) if installed else None,
    )
