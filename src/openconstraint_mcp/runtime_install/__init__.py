from __future__ import annotations

from .core import (
    MANAGED_RUNTIME_MARKER,
    MINIZINC_VERSION,
    RuntimeInstallError,
    check_supported_platform,
    install_managed_runtime,
    is_managed_runtime_dir,
)

__all__ = [
    "MANAGED_RUNTIME_MARKER",
    "MINIZINC_VERSION",
    "RuntimeInstallError",
    "check_supported_platform",
    "install_managed_runtime",
    "is_managed_runtime_dir",
]
