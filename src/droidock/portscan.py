"""Bounded ADB protocol discovery for one explicitly selected IP address, without CLI dependencies."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from .errors import DroidockError
from .models import normalize_endpoint


class PortScanStatus(StrEnum):
    RUNNING = "running"
    FOUND = "found"
    NOT_FOUND = "not_found"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PortScanProgress:
    host: str
    completed: int
    total: int
    elapsed_seconds: float
    status: PortScanStatus = PortScanStatus.RUNNING
    endpoint: str | None = None

    @property
    def eta_seconds(self) -> float | None:
        """Estimated time to check the remaining range, not a promise of finding ADB."""
        if self.status != PortScanStatus.RUNNING:
            return 0.0
        if not self.completed or self.elapsed_seconds < 1:
            return None
        return max(0.0, (self.total - self.completed) * self.elapsed_seconds / self.completed)


def endpoint_host(endpoint: str) -> str | None:
    """Return the canonical IP of an ADB endpoint, or None for USB/DNS transports."""
    try:
        parsed = urlsplit("//" + normalize_endpoint(endpoint))
        return str(ipaddress.ip_address(parsed.hostname)) if parsed.hostname else None
    except (DroidockError, ValueError):
        return None


def preferred_adb_ports(host: str, endpoints: Iterable[str]) -> tuple[int, ...]:
    """Reuse current or saved ports only when their address matches the selected IP."""
    host = str(ipaddress.ip_address(host))
    ports = []
    for endpoint in endpoints:
        if endpoint_host(endpoint) == host:
            port = urlsplit("//" + normalize_endpoint(endpoint)).port
            if port is not None:
                ports.append(port)
    return tuple(dict.fromkeys(ports))


def _ports(values: Iterable[int]) -> list[int]:
    result = []
    for port in values:
        if type(port) is not int or not 1 <= port <= 65535:
            raise DroidockError("TCP ports must be integers from 1 to 65535.", code="invalid_port")
        result.append(port)
    return list(dict.fromkeys(result))


class AdbPortScanner:
    """Find ADB on one IP; never pair, connect through ADB, or update a saved profile.

    Await scan() from an async application, or use asyncio.run(scanner.scan(...)).
    Progress callbacks run on the calling event loop. Set stop or cancel the task to stop all probes.
    A protocol response establishes a candidate; callers must still authorize and verify device identity.
    """

    def __init__(
        self,
        *,
        concurrency: int = 192,
        connect_timeout: float = 0.8,
        response_timeout: float = 2.0,
        total_timeout: float = 300.0,
    ) -> None:
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise DroidockError("Scan concurrency must be between 1 and 256.", code="invalid_setting")
        for value in (connect_timeout, response_timeout, total_timeout):
            if not math.isfinite(value) or not 0 < value <= 3600:
                raise DroidockError(
                    "Scan timeouts must be finite and between 0 and 3600 seconds.", code="invalid_setting"
                )
        self.concurrency = concurrency
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.total_timeout = total_timeout

    async def _probe(self, host: str, port: int) -> bool:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), self.connect_timeout)
            body = b"host::\0"
            command = int.from_bytes(b"CNXN", "little")
            packet = (
                struct.pack(
                    "<6I", command, 0x01000001, 1024 * 1024, len(body), sum(body), command ^ 0xFFFFFFFF
                )
                + body
            )
            async with asyncio.timeout(self.response_timeout):
                writer.write(packet)
                await writer.drain()
                header = await reader.readexactly(24)
            response = struct.unpack("<6I", header)
            return (
                header[:4] in {b"AUTH", b"STLS", b"CNXN"}
                and response[5] == response[0] ^ 0xFFFFFFFF
                and response[3] <= 1024 * 1024
            )
        except (OSError, asyncio.IncompleteReadError):
            return False
        finally:
            if writer is not None:
                writer.close()
                # No connection is retained, including on cancellation or a non-ADB response.
                writer.transport.abort()

    async def scan(
        self,
        host: str,
        *,
        preferred_ports: Iterable[int] = (),
        ports: Iterable[int] | None = None,
        excluded_ports: Iterable[int] = (),
        on_progress: Callable[[PortScanProgress], None] | None = None,
        stop: threading.Event | None = None,
    ) -> PortScanProgress:
        """Check saved ports first, then the requested range (all TCP ports by default).

        A timeout/cancellation can leave ports unchecked. NOT_FOUND means no probe recognized ADB;
        firewalls, a sleeping device, or slow responses can also cause that result.
        """
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError as exc:
            raise DroidockError("Select a single valid device IP address.", code="invalid_endpoint") from exc
        excluded = set(_ports(excluded_ports))
        preferred = [p for p in _ports(preferred_ports) if p not in excluded]
        preferred_set = set(preferred)
        requested = _ports(
            ports if ports is not None else [5555, *range(32768, 65536), *range(1024, 32768), *range(1, 1024)]
        )
        remaining = [p for p in requested if p not in excluded and p not in preferred_set]
        total = len(preferred) + len(remaining)
        started = time.monotonic()
        completed = 0
        found: str | None = None
        status = PortScanStatus.RUNNING
        stop = stop or threading.Event()
        last_report = 0.0

        def report(*, force: bool = False) -> PortScanProgress:
            nonlocal last_report
            now = time.monotonic()
            event = PortScanProgress(host, completed, total, now - started, status, found)
            if on_progress and (force or now - last_report >= 0.2):
                on_progress(event)
                last_report = now
            return event

        def finished() -> bool:
            return found is not None or stop.is_set() or time.monotonic() - started >= self.total_timeout

        async def phase(candidates: list[int]) -> None:
            nonlocal completed, found
            if not candidates or finished():
                return
            iterator = iter(candidates)

            async def worker() -> None:
                nonlocal completed, found
                while not finished():
                    port = next(iterator, None)
                    if port is None:
                        return
                    recognized = await self._probe(host, port)
                    completed += 1
                    if recognized:
                        found = normalize_endpoint(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
                        return

            tasks = [asyncio.create_task(worker()) for _ in range(min(self.concurrency, len(candidates)))]
            pending = set(tasks)
            try:
                while pending and not finished():
                    done, pending = await asyncio.wait(
                        pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                    report()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        report(force=True)
        try:
            await phase(preferred)
            await phase(remaining)
        except asyncio.CancelledError:
            status = PortScanStatus.CANCELLED
            report(force=True)
            raise
        if found:
            status = PortScanStatus.FOUND
        elif stop.is_set():
            status = PortScanStatus.CANCELLED
        elif completed < total:
            status = PortScanStatus.TIMED_OUT
        else:
            status = PortScanStatus.NOT_FOUND
        return report(force=True)
