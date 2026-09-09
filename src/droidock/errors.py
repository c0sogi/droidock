from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import CommandResult


class DroidockError(Exception):
    """A recoverable operation failure; callers can branch on ``code``."""

    def __init__(self, message: str, *, code: str = "connection_error") -> None:
        super().__init__(message)
        self.code = code


class IdentityError(DroidockError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="identity_mismatch")


class SelectionError(DroidockError):
    """Selection failed; candidate IDs/addresses can be presented by any UI."""

    def __init__(self, message: str, *, code: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message, code=code)
        self.candidates = candidates


class CommandError(DroidockError):
    """A nonzero ADB exit with a structured, redacted result."""

    def __init__(self, result: CommandResult, *, code: str = "adb_failed") -> None:
        super().__init__(result.output[-2500:] or "The ADB command failed.", code=code)
        self.result = result
