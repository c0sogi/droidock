from __future__ import annotations

import asyncio
import struct
import threading
import time
from contextlib import asynccontextmanager

import pytest

from droidock import AdbPortScanner, DroidockError, PortScanProgress, PortScanStatus, preferred_adb_ports


def header(kind=b"STLS", *, magic=None, length=0):
    command = int.from_bytes(kind, "little")
    return struct.pack("<6I", command, 0, 0, length, 0, command ^ 0xFFFFFFFF if magic is None else magic)


@asynccontextmanager
async def server(response: bytes):
    requests: list[bytes] = []
    clients: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        assert task is not None
        clients.add(task)
        try:
            request = await reader.readexactly(24)
            requests.append(request)
            await reader.readexactly(struct.unpack("<6I", request)[3])
            # Fragment the reply to exercise TCP stream reads rather than assuming packet boundaries.
            writer.write(response[:8])
            await writer.drain()
            writer.write(response[8:])
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            clients.discard(task)

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield listener.sockets[0].getsockname()[1], requests
    finally:
        listener.close()
        await listener.wait_closed()
        if clients:
            await asyncio.wait_for(asyncio.gather(*clients), 1)


@pytest.mark.parametrize("kind", [b"STLS", b"AUTH", b"CNXN"])
def test_real_tcp_probe_recognizes_adb_without_pairing_or_shell_commands(kind):
    async def run():
        async with server(header(kind)) as (port, requests):
            events = []
            result = await AdbPortScanner().scan("127.0.0.1", ports=[port], on_progress=events.append)
            assert result.status is PortScanStatus.FOUND
            assert result.endpoint == f"127.0.0.1:{port}"
            assert result.completed == 1
            assert events[0].completed == 0
            assert events[-1] == result
            assert requests[0][:4] == b"CNXN"

    asyncio.run(run())


@pytest.mark.parametrize(
    "response", [b"HTTP/1.1 200 OK\r\n\r\nHello", header(magic=123), header(length=2**31), b"STLS", b""]
)
def test_open_non_adb_or_unresponsive_ports_are_not_reported_as_adb(response):
    async def run():
        async with server(response) as (port, _):
            result = await AdbPortScanner(response_timeout=0.05).scan("127.0.0.1", ports=[port])
            assert result.status is PortScanStatus.NOT_FOUND
            assert result.endpoint is None
            assert result.completed == 1

    asyncio.run(run())


def test_saved_port_success_stops_before_scanning_the_range(monkeypatch):
    scanner = AdbPortScanner()
    attempted = []

    async def probe(host, port):
        attempted.append((host, port))
        return port == 40001

    monkeypatch.setattr(scanner, "_probe", probe)
    result = asyncio.run(
        scanner.scan(
            "100.64.0.10",
            preferred_ports=[40001, 40001],
            ports=[1, 40001, 40002, 40003],
            excluded_ports=[1],
        )
    )
    assert attempted == [("100.64.0.10", 40001)]
    assert result.status is PortScanStatus.FOUND
    assert result.completed == 1
    assert result.total == 3


def test_unavailable_saved_port_falls_back_and_preserves_ipv6(monkeypatch):
    scanner = AdbPortScanner(concurrency=1)
    attempted = []

    async def probe(host, port):
        attempted.append(port)
        return port == 40002

    monkeypatch.setattr(scanner, "_probe", probe)
    result = asyncio.run(
        scanner.scan(
            "fd7a:115c:a1e0::10", preferred_ports=[40001], ports=[1, 40002, 40003], excluded_ports=[1]
        )
    )
    assert attempted == [40001, 40002]
    assert result.endpoint == "[fd7a:115c:a1e0::10]:40002"
    assert result.completed == 2
    assert result.total == 3


def test_current_ports_are_only_reused_for_the_selected_host():
    assert preferred_adb_ports(
        "100.64.0.10",
        [
            "USB-A",
            "100.64.0.10:40001",
            "100.64.0.11:40002",
            "xr.example:40003",
            "100.64.0.10:40001",
        ],
    ) == (40001,)
    assert preferred_adb_ports("fd7a:115c:a1e0::10", ["[fd7a:115c:a1e0:0:0:0:0:10]:40001"]) == (40001,)


@pytest.mark.parametrize("mode", ["deadline", "stop", "task_cancel"])
def test_cancellation_and_deadline_close_all_in_flight_probes(monkeypatch, mode):
    scanner = AdbPortScanner(concurrency=3, total_timeout=0.05 if mode == "deadline" else 10)
    active = 0
    peak = 0
    entered = asyncio.Event()
    stop = threading.Event()
    events = []

    async def probe(host, port):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
        return False

    monkeypatch.setattr(scanner, "_probe", probe)

    async def run():
        task = asyncio.create_task(
            scanner.scan("127.0.0.1", ports=range(1, 100), stop=stop, on_progress=events.append)
        )
        await entered.wait()
        if mode == "stop":
            stop.set()
        if mode == "task_cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == (
                PortScanStatus.TIMED_OUT if mode == "deadline" else PortScanStatus.CANCELLED
            )
            assert result.completed == 0  # Cancelled sockets were not checked to completion.
            assert result.total == 99
        assert active == 0
        assert peak == 3
        assert events[-1].status != PortScanStatus.RUNNING

    started = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - started < 2


def test_already_cancelled_scan_does_no_network_io(monkeypatch):
    scanner = AdbPortScanner()
    stop = threading.Event()
    stop.set()

    async def forbidden(*args):
        pytest.fail("Cancelled scan opened a socket")

    monkeypatch.setattr(scanner, "_probe", forbidden)
    result = asyncio.run(scanner.scan("127.0.0.1", stop=stop))
    assert result.status is PortScanStatus.CANCELLED
    assert result.completed == 0


def test_eta_estimates_remaining_range_and_early_finish_keeps_actual_count():
    assert PortScanProgress("127.0.0.1", 0, 1000, 1).eta_seconds is None
    assert PortScanProgress("127.0.0.1", 100, 1000, 10).eta_seconds == 90
    result = PortScanProgress("127.0.0.1", 100, 1000, 10, PortScanStatus.FOUND, "127.0.0.1:40001")
    assert result.eta_seconds == 0
    assert result.completed == 100


@pytest.mark.parametrize(
    "settings",
    [
        {"concurrency": 0},
        {"concurrency": 257},
        {"connect_timeout": 0},
        {"response_timeout": float("nan")},
        {"total_timeout": float("inf")},
    ],
)
def test_scan_configuration_is_bounded(settings):
    with pytest.raises(DroidockError) as error:
        AdbPortScanner(**settings)
    assert error.value.code == "invalid_setting"


@pytest.mark.parametrize(
    "host,ports", [("not-an-ip", [1]), ("127.0.0.1", [0]), ("127.0.0.1", [65536]), ("127.0.0.1", [True])]
)
def test_invalid_scan_targets_are_rejected(host, ports):
    with pytest.raises(DroidockError):
        asyncio.run(AdbPortScanner().scan(host, ports=ports))
