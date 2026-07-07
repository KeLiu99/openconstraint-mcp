from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, field_validator


class RuntimeStatus(BaseModel):
    installed: bool
    runtime_dir: str
    minizinc_binary: str | None = None


class InstallConfig(BaseModel):
    runtime_dir: str

    @field_validator("runtime_dir")
    @classmethod
    def _runtime_dir_must_be_absolute(cls, value: str) -> str:
        if not value or not Path(value).is_absolute():
            raise ValueError("runtime_dir must be a non-empty absolute path")
        return value
