from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import CommandResult, PairResult, Service, Transport


class Backend(Protocol):
    """Replace this adapter for tests or an alternative Android transport implementation."""

    def transports(self) -> list[Transport]: ...
    def inspect(self, address: str) -> Transport: ...
    def connect(self, endpoint: str) -> None: ...
    def pair(self, endpoint: str, code: str) -> PairResult: ...
    def disconnect(self, endpoint: str) -> None: ...
    def reconnect(self, address: str) -> None: ...
    def services(self) -> list[Service]: ...
    def diagnostics(self) -> dict[str, str]: ...


class Discovery(Protocol):
    def discover(self, seconds: float) -> tuple[list[Service], list[str]]: ...


@runtime_checkable
class CommandBackend(Protocol):
    """Optional command capability; discovery-only test/custom backends remain valid."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        serial: str | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        check: bool = True,
    ) -> CommandResult: ...
