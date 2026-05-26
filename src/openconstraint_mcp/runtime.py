from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from platformdirs import PlatformDirs
from pydantic import ValidationError

from .schemas import InstallConfig, RuntimeStatus

_APP_NAME = "openconstraint-mcp"
_ENV_RUNTIME_DIR = "OPENCONSTRAINT_MCP_RUNTIME_DIR"


class RuntimeMissingError(RuntimeError):
    """Raised when the managed MiniZinc runtime is not available."""


def _config_path() -> Path:
    dirs = PlatformDirs(_APP_NAME, _APP_NAME)
    return Path(dirs.user_config_dir) / "install.json"


def _load_install_config() -> tuple[InstallConfig | None, str | None]:
    """Read the persisted install config.

    Returns ``(config, warning)``. ``warning`` is a human-readable string only
    when a config file is present but cannot be parsed or fails validation —
    distinct from the common "no config yet" case, which returns ``(None, None)``
    and silently falls back to the default runtime location.
    """
    path = _config_path()
    try:
        raw = path.read_text()
    except OSError:
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, (
            f"ignoring corrupt install config at {path} (invalid JSON); "
            "falling back to the default runtime location"
        )
    try:
        return InstallConfig.model_validate(data), None
    except ValidationError:
        return None, (
            f"ignoring corrupt install config at {path} (failed validation); "
            "falling back to the default runtime location"
        )


def read_install_config() -> InstallConfig | None:
    return _load_install_config()[0]


def install_config_warning() -> str | None:
    """A warning to surface iff the persisted install config exists but is corrupt."""
    return _load_install_config()[1]


def write_install_config(runtime_dir: Path) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = InstallConfig(runtime_dir=str(runtime_dir.resolve()))
    path.write_text(config.model_dump_json(indent=2) + "\n")


def get_runtime_dir() -> Path:
    override = os.environ.get(_ENV_RUNTIME_DIR)
    if override:
        return Path(override).expanduser()
    config = read_install_config()
    if config is not None:
        return Path(config.runtime_dir)
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
