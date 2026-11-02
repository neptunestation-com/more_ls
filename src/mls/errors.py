"""Errors are written for an LLM to act on: what is wrong, where (JSON path), and the fix."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MlsError:
    path: str
    message: str
    fix: str = ""
    code: str = "error"

    def __str__(self) -> str:
        s = f"{self.code} at {self.path}: {self.message}"
        if self.fix:
            s += f"\n  fix: {self.fix}"
        return s

    def to_dict(self) -> dict:
        return {"code": self.code, "path": self.path, "message": self.message, "fix": self.fix}


class SpecError(Exception):
    """Raised when a spec fails validation, typechecking or the fragment check."""

    def __init__(self, errors: list[MlsError]):
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


class CompileError(Exception):
    """Raised when a target cannot enforce a construct soundly (never silently weakened)."""

    def __init__(self, errors: list[MlsError]):
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


@dataclass
class ErrorSink:
    errors: list[MlsError] = field(default_factory=list)

    def add(self, path: str, message: str, fix: str = "", code: str = "error") -> None:
        self.errors.append(MlsError(path=path, message=message, fix=fix, code=code))

    def raise_if_any(self, cls: type[Exception] = SpecError) -> None:
        if self.errors:
            raise cls(self.errors)
