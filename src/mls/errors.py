"""Errors are written for an LLM to act on: what is wrong, where (JSON path), and the fix.
When the input was terse (`.mls`, D50) the error also carries a source span: file, line, column
and the offending line, looked up from the JSON path; the path stays the key."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Span:
    file: str
    line: int
    col: int
    text: str = ""  # the source line

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "col": self.col, "text": self.text}


@dataclass
class MlsError:
    path: str
    message: str
    fix: str = ""
    code: str = "error"
    span: Span | None = None

    def __str__(self) -> str:
        where = f"{self.path} ({self.span})" if self.span else self.path
        s = f"{self.code} at {where}: {self.message}"
        if self.span and self.span.text.strip():
            s += f"\n    {self.span.text.strip()}"
        if self.fix:
            s += f"\n  fix: {self.fix}"
        return s

    def to_dict(self) -> dict:
        return {"code": self.code, "path": self.path, "message": self.message, "fix": self.fix,
                "span": self.span.to_dict() if self.span else None}


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
