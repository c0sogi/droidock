from __future__ import annotations

import io
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import connected
from rich.console import Console
from typer.testing import CliRunner

from droidock import (
    QUALITY_LEVELS,
    AdbBackend,
    DeviceCriteria,
    DroidockError,
    Identity,
    LinkMeasurement,
    MirrorOptions,
    MirrorReport,
    MirrorSession,
    ScrcpyInstall,
    Service,
    ServiceKind,
    Transport,
    choose_quality,
    quality_index,
    scrcpy_arguments,
)
from droidock._windows import WindowPlacement
from droidock.cli import app
from droidock.interactive import InteractiveCli
from droidock.mirror import AdbLinkProbe, LinkMonitor, MirrorStatus
from droidock.mirror_view import run_mirror, select_device, status_line

INSTALL = ScrcpyInstall(Path("scrcpy.exe"), "5.0.1", "managed")
NETWORK = Transport("192.168.0.6:40001", "device", Identity("SERIAL-XR", "samsung", "SM-I610", "guid-xr"))
USB = connected("USB-XR", "SERIAL-XR", "SM-I610", "guid-xr")
runner = CliRunner()


def value(arguments: list[str], option: str) -> str:
    return next(a.split("=", 1)[1] for a in arguments if a.startswith(option + "="))


class FakeProcess:
    """Exits with `code` after its output is read and `polls` more polls, or with 0 when stopped."""

    def __init__(self, pid: int, lines: tuple[str, ...] = (), *, polls: int | None = None, code: int = 0):
        payload = "".join(line + "\n" for line in lines).encode()
        self.pid = pid
        self.stdout = io.BytesIO(payload)
        self._size = len(payload)
        self.remaining = polls
        self.exit_code = code
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self):
        if self.returncode is None and self.remaining is not None and self.stdout.tell() >= self._size:
            if self.remaining <= 0:
                self.returncode = self.exit_code
            else:
                self.remaining -= 1
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("scrcpy", timeout or 0)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0

    def kill(self):
        self.returncode = -9


class Launcher:
    def __init__(self, *processes: FakeProcess):
        self.processes = list(processes)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, arguments, environment):
        self.calls.append((list(arguments), dict(environment)))
        return self.processes.pop(0)


class FakeProbe:
    def __init__(self, throughput: float | None = None, round_trip: float = 0.004):
        self.rate = throughput
        self.rtt = round_trip
        self.throughput_calls = 0

    def round_trip(self, timeout=2.5):
        return self.rtt

    def throughput(self, *, budget=2.0):
        self.throughput_calls += 1
        return self.rate


class FakeWindows:
    def __init__(self):
        self.captured: list[int] = []
        self.restored: list[tuple[int, WindowPlacement]] = []

    def capture(self, pid):
        self.captured.append(pid)
        return WindowPlacement(100, 50, 1280, 720)

    def restore(self, pid, placement, *, timeout=15.0):
        self.restored.append((pid, placement))
        return True


def monitors(*congestion: bool, delay: float = 500.0):
    """A LinkMonitor stand-in per stream; each reports the given congestion on its first check."""
    created: list[object] = []
    flags = list(congestion)

    class Monitor:
        def __init__(self, probe, **kwargs):
            self.congested = flags.pop(0) if flags else False
            self.recent_delay_ms = delay
            self.delay_ms = delay
            self.rearmed = 0
            created.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def rearm(self):
            self.rearmed += 1
            self.congested = False

    return Monitor, created


def make_session(manager, transport, launcher, **overrides):
    probe = overrides.pop("probe", FakeProbe())
    events = overrides.pop("events", None)
    return MirrorSession(
        manager,
        transport,
        install=INSTALL,
        launcher=launcher,
        windows=overrides.pop("windows", None),
        probe_factory=lambda serial, port: probe,
        on_event=events.append if events is not None else None,
        sleep=lambda seconds: time.sleep(0.001),
        **overrides,
    )


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def test_quality_ladder_descends_by_bit_rate():
    rates = [level.bit_rate for level in QUALITY_LEVELS]
    assert rates == sorted(rates, reverse=True)
    assert len({level.name for level in QUALITY_LEVELS}) == len(QUALITY_LEVELS)
    assert QUALITY_LEVELS[3].label == "balanced (1280px, 30 fps, 5 Mb/s)"
    assert QUALITY_LEVELS[5].label == "low (800px, 30 fps, 1.8 Mb/s)"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (LinkMeasurement(True), "ultra"),
        (LinkMeasurement(False), "balanced"),
        (LinkMeasurement(False, 200_000_000, 4.0), "high"),
        (LinkMeasurement(False, 15_000_000, 4.0), "balanced"),
        (LinkMeasurement(False, 6_000_000, 4.0), "low"),
        (LinkMeasurement(False, 1_000_000, 4.0), "minimal"),
        (LinkMeasurement(False, 200_000_000, 120.0), "balanced"),
    ],
)
def test_starting_quality_leaves_headroom_on_networks(link, expected):
    assert QUALITY_LEVELS[choose_quality(link)].name == expected


def test_quality_names_and_positions():
    assert quality_index("Balanced") == 3
    assert quality_index(0) == 0
    for invalid in ("fastest", 7, -1, True):
        with pytest.raises(DroidockError) as raised:
            quality_index(invalid)
        assert raised.value.code == "invalid_quality"


def test_scrcpy_arguments_favour_latency():
    arguments = scrcpy_arguments("192.168.0.6:40001", QUALITY_LEVELS[3], MirrorOptions())
    assert arguments[:6] == [
        "--serial=192.168.0.6:40001",
        "--video-codec=h264",
        "--max-size=1280",
        "--max-fps=30",
        "--video-bit-rate=5000000",
        "--print-fps",
    ]
    assert "--no-audio" in arguments and "--disable-screensaver" in arguments
    assert "--no-control" not in arguments and "--fullscreen" not in arguments
    assert not any(a.startswith("--video-buffer") for a in arguments)


def test_scrcpy_arguments_reflect_options_and_installed_support():
    options = MirrorOptions(
        control=False,
        audio=True,
        title="Galaxy XR",
        always_on_top=True,
        fullscreen=True,
        scrcpy_arguments=("--crop=1224:1440:0:0",),
    )
    arguments = scrcpy_arguments("USB-XR", QUALITY_LEVELS[0], options)
    assert {"--no-control", "--window-title=Galaxy XR", "--always-on-top", "--fullscreen"} <= set(arguments)
    assert "--no-audio" not in arguments
    assert arguments[-1] == "--crop=1224:1440:0:0"

    restored = scrcpy_arguments(
        "USB-XR", QUALITY_LEVELS[0], options, placement=WindowPlacement(0, 0, 1920, 1080, True)
    )
    assert "--fullscreen" not in restored
    assert {"--window-borderless", "--no-window-aspect-ratio-lock"} <= set(restored)

    older = ScrcpyInstall(Path("scrcpy"), "3.1", "path", frozenset({"serial", "max-size", "no-audio"}))
    limited = scrcpy_arguments(
        "USB-XR", QUALITY_LEVELS[0], MirrorOptions(scrcpy_arguments=("--x",)), install=older
    )
    assert limited == ["--serial=USB-XR", "--max-size=1920", "--no-audio", "--x"]


class FakeAdbServer:
    """Answers the smart-socket requests the probe sends, pacing transfers in 64 KiB chunks."""

    def __init__(self, serial: str = "SERIAL-XR", *, chunk_delay: float = 0.001):
        self.serial = serial
        self.chunk_delay = chunk_delay
        self.requests: list[str] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _exact(connection, count):
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _request(self, connection) -> str:
        request = self._exact(connection, int(self._exact(connection, 4), 16)).decode()
        self.requests.append(request)
        return request

    def _serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                try:
                    if self._request(connection) != f"host:transport:{self.serial}":
                        message = b"device not found"
                        connection.sendall(b"FAIL" + f"{len(message):04x}".encode() + message)
                        continue
                    connection.sendall(b"OKAY")
                    service = self._request(connection)
                    connection.sendall(b"OKAY")
                    if service.startswith("shell:echo"):
                        connection.sendall(b"droidock\n")
                    elif service.startswith("exec:dd"):
                        match = re.search(r"count=(\d+)", service)
                        blocks = int(match.group(1)) if match else 0
                        for _ in range(blocks):
                            connection.sendall(b"\0" * 65536)
                            time.sleep(self.chunk_delay)
                except OSError:
                    pass

    def close(self):
        self.listener.close()


def test_probe_uses_the_server_protocol_for_round_trips_and_throughput():
    server = FakeAdbServer()
    try:
        probe = AdbLinkProbe("SERIAL-XR", port=server.port)
        assert 0 < probe.round_trip(2.0) < 2.0
        rate = probe.throughput(budget=3.0)
        assert rate is not None and rate > 10_000_000
        assert server.requests[:2] == ["host:transport:SERIAL-XR", "shell:echo droidock"]
        transfers = [r for r in server.requests if r.startswith("exec:dd")]
        assert transfers[0] == "exec:dd if=/dev/zero bs=65536 count=2 2>/dev/null"
        assert len(transfers) >= 2  # stops early once a transfer takes 0.2 s
        with pytest.raises(DroidockError) as raised:
            AdbLinkProbe("OTHER", port=server.port).round_trip(2.0)
        assert raised.value.code == "adb_failed" and "device not found" in str(raised.value)
    finally:
        server.close()


def test_probe_reports_a_server_that_hangs_up():
    listener = socket.create_server(("127.0.0.1", 0))

    def hang_up():
        connection, _ = listener.accept()
        connection.close()

    threading.Thread(target=hang_up, daemon=True).start()
    try:
        with pytest.raises(DroidockError) as raised:
            AdbLinkProbe("SERIAL-XR", port=listener.getsockname()[1]).round_trip(2.0)
        assert raised.value.code == "connection_error"
    finally:
        listener.close()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SilentProbe:
    def round_trip(self, timeout=2.5):
        raise DroidockError("unused", code="timeout")


def test_monitor_settles_then_flags_sustained_queueing():
    monitor = LinkMonitor(SilentProbe(), base_ms=10.0, clock=Clock())  # type: ignore[arg-type]
    monitor.record(900.0)  # start-up backlog while settling is ignored
    assert not monitor.congested and monitor.delay_ms == 890.0
    monitor.record(40.0)  # drained: armed
    monitor.record(500.0)
    monitor.record(520.0)
    monitor.record(60.0)  # a fast sample resets the streak
    assert not monitor.congested
    for sample in (400.0, 450.0, 470.0):
        monitor.record(sample)
    assert monitor.congested
    assert monitor.recent_delay_ms == 440.0


def test_monitor_counts_timeouts_and_rearms_after_restarts():
    clock = Clock()
    monitor = LinkMonitor(SilentProbe(), base_ms=10.0, timeout=2.0, clock=clock)  # type: ignore[arg-type]
    clock.now = 13.0
    monitor.record(None)  # the settle period has passed: armed, then counting starts
    for _ in range(3):
        monitor.record(None)
    assert monitor.congested and monitor.delay_ms == 2000.0
    monitor.rearm()
    assert not monitor.congested
    monitor.record(5.0)
    assert monitor.base_ms == 5.0 and monitor.delay_ms == 0.0


def test_monitor_thread_samples_the_probe():
    class SlowLink:
        def round_trip(self, timeout=2.5):
            return 0.5

    monitor = LinkMonitor(SlowLink(), base_ms=10.0, interval=0.01, settle_seconds=0.0)  # type: ignore[arg-type]
    monitor.start()
    try:
        assert wait_for(lambda: monitor.congested)
    finally:
        monitor.stop()
    assert monitor.delay_ms == 490.0


def test_session_runs_until_the_window_closes(rig):
    manager, _, _ = rig
    process = FakeProcess(11, ("\x1b[0mINFO: Texture: 1920x1080", "INFO: 60 fps"), polls=1)
    launcher = Launcher(process)
    session = make_session(manager, USB, launcher)
    report = session.run()
    assert report.reason == "closed" and report.restarts == 0
    arguments, environment = launcher.calls[0]
    assert arguments[0] == "scrcpy.exe"
    assert value(arguments, "--max-size") == "1920" and value(arguments, "--serial") == "USB-XR"
    assert environment["ANDROID_ADB_SERVER_PORT"] == "5037"
    status = session.status
    assert (status.state, status.fps, status.video_size, status.delay_ms) == (
        "stopped",
        60,
        "1920x1080",
        None,
    )


def test_network_links_are_measured_before_starting(rig):
    manager, _, _ = rig
    probe = FakeProbe(throughput=15_000_000)
    events: list = []
    launcher = Launcher(FakeProcess(12, ("INFO: 30 fps",), polls=0))
    report = make_session(manager, NETWORK, launcher, probe=probe, events=events).run()
    arguments = launcher.calls[0][0]
    assert (value(arguments, "--max-size"), value(arguments, "--max-fps")) == ("1280", "30")
    assert value(arguments, "--video-bit-rate") == "5000000"
    assert report.quality.name == "balanced" and probe.throughput_calls == 1
    assert any("Network, 15.0 Mb/s, round trip 4 ms" in e.message for e in events if e.kind == "quality")


def test_a_fixed_quality_skips_the_throughput_measurement(rig):
    manager, _, _ = rig
    probe = FakeProbe(throughput=200_000_000)
    launcher = Launcher(FakeProcess(13, polls=0))
    make_session(manager, NETWORK, launcher, probe=probe, options=MirrorOptions(quality="low")).run()
    assert value(launcher.calls[0][0], "--max-size") == "800"
    assert probe.throughput_calls == 0


def test_queueing_restarts_at_a_lower_quality_in_the_same_place(rig, monkeypatch):
    manager, _, _ = rig
    monitor, created = monitors(True, False)
    monkeypatch.setattr("droidock.mirror.LinkMonitor", monitor)
    first = FakeProcess(21, ("INFO: 30 fps",))
    second = FakeProcess(22, ("INFO: 30 fps",), polls=2)
    launcher = Launcher(first, second)
    windows = FakeWindows()
    events: list = []
    session = make_session(
        manager, NETWORK, launcher, probe=FakeProbe(throughput=15_000_000), windows=windows, events=events
    )
    report = session.run()
    assert report.restarts == 1 and report.reason == "closed" and report.quality.name == "light"
    assert first.signals and len(created) == 2
    assert value(launcher.calls[1][0], "--max-size") == "1024"
    assert windows.captured == [21]
    assert wait_for(lambda: windows.restored == [(22, WindowPlacement(100, 50, 1280, 720))])
    assert any(e.kind == "congestion" and "500 ms" in e.message for e in events)


def test_severe_queueing_skips_a_step(rig, monkeypatch):
    manager, _, _ = rig
    monitor, _ = monitors(True, delay=1500.0)
    monkeypatch.setattr("droidock.mirror.LinkMonitor", monitor)
    launcher = Launcher(FakeProcess(31), FakeProcess(32, polls=0))
    make_session(manager, NETWORK, launcher, probe=FakeProbe(throughput=15_000_000)).run()
    assert value(launcher.calls[1][0], "--max-size") == "800"


def test_the_lowest_quality_warns_once_instead_of_restarting(rig, monkeypatch):
    manager, _, _ = rig
    monitor, created = monitors(True)
    monkeypatch.setattr("droidock.mirror.LinkMonitor", monitor)
    events: list = []
    launcher = Launcher(FakeProcess(41, polls=5))
    make_session(manager, NETWORK, launcher, events=events, options=MirrorOptions(quality="minimal")).run()
    assert len(launcher.calls) == 1
    assert [e.kind for e in events].count("warning") == 1
    assert created[0].rearmed == 1  # type: ignore[attr-defined]


def test_usb_and_fixed_sessions_do_not_monitor(rig, monkeypatch):
    manager, _, _ = rig
    monitor, created = monitors(True)
    monkeypatch.setattr("droidock.mirror.LinkMonitor", monitor)
    make_session(manager, USB, Launcher(FakeProcess(51, polls=0))).run()
    make_session(
        manager, NETWORK, Launcher(FakeProcess(52, polls=0)), options=MirrorOptions(adaptive=False)
    ).run()
    assert created == []


def test_disconnections_reconnect_and_resume(rig):
    manager, backend, _ = rig
    backend.online[NETWORK.address] = NETWORK
    events: list = []
    launcher = Launcher(FakeProcess(61, ("INFO: 30 fps",), polls=0, code=2), FakeProcess(62, polls=0))
    report = make_session(
        manager, NETWORK, launcher, events=events, options=MirrorOptions(adaptive=False)
    ).run()
    assert report.reconnects == 1 and report.reason == "closed"
    assert [e.kind for e in events if e.kind in {"disconnected", "reconnected"}] == [
        "disconnected",
        "reconnected",
    ]
    assert manager.store.read().devices == []  # reconnecting does not create profiles


def test_disconnections_end_the_session_without_reconnect(rig):
    manager, _, _ = rig
    launcher = Launcher(FakeProcess(71, ("INFO: 30 fps",), polls=0, code=2))
    report = make_session(manager, USB, launcher, options=MirrorOptions(reconnect=False)).run()
    assert report.reason == "disconnected" and len(launcher.calls) == 1


def test_quit_while_reconnecting(rig):
    manager, _, _ = rig
    commands = iter([None, "quit"])
    launcher = Launcher(FakeProcess(81, ("INFO: 30 fps",), polls=0, code=2))
    report = make_session(manager, USB, launcher, commands=lambda: next(commands, "quit")).run()
    assert report.reason == "quit" and report.reconnects == 0


def test_start_failures_report_scrcpy_errors(rig):
    manager, _, _ = rig
    lines = ("INFO: scrcpy 5.0.1", "ERROR: Could not find any ADB device", "[server] ERROR: Encoder failed")
    launcher = Launcher(FakeProcess(91, lines, polls=0, code=1))
    events: list = []
    with pytest.raises(DroidockError) as raised:
        make_session(manager, USB, launcher, events=events).run()
    assert raised.value.code == "mirror_failed"
    assert "Could not find any ADB device" in str(raised.value) and "Encoder failed" in str(raised.value)
    assert [e.kind for e in events].count("scrcpy") == 2


def test_keys_change_quality_and_quit(rig):
    manager, _, _ = rig
    keys = iter([None, "down", None, "up", None, None, "quit"])
    first, second, third = FakeProcess(101), FakeProcess(102), FakeProcess(103)
    launcher = Launcher(first, second, third)
    windows = FakeWindows()
    report = make_session(manager, USB, launcher, windows=windows, commands=lambda: next(keys, None)).run()
    assert report.reason == "quit" and report.restarts == 2
    assert [value(call[0], "--max-size") for call in launcher.calls] == ["1920", "1600", "1920"]
    assert first.signals and second.signals and third.signals
    assert windows.captured == [101, 102]


def test_environment_points_scrcpy_at_the_same_adb_server(rig, monkeypatch):
    manager, _, _ = rig
    manager.configure(server_port=5099)
    monkeypatch.setattr(AdbBackend, "executable", property(lambda self: Path("C:/tools/adb.exe")))
    manager.backend = AdbBackend(manager.settings)
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:remote:5037")
    monkeypatch.setenv("ANDROID_SERIAL", "SOMETHING-ELSE")
    launcher = Launcher(FakeProcess(111, polls=0))
    make_session(manager, USB, launcher).run()
    environment = launcher.calls[0][1]
    assert environment["ADB"] == str(Path("C:/tools/adb.exe"))
    assert environment["ANDROID_ADB_SERVER_PORT"] == "5099"
    assert "ADB_SERVER_SOCKET" not in environment and "ANDROID_SERIAL" not in environment


def test_sessions_require_a_verified_device(rig):
    manager, _, _ = rig
    with pytest.raises(DroidockError) as raised:
        make_session(manager, Transport("USB-X", "unauthorized"), Launcher())
    assert raised.value.code == "unverified_device"
    with pytest.raises(DroidockError):
        make_session(manager, USB, Launcher(), options=MirrorOptions(quality="cinema"))


def test_status_line_summarises_the_stream():
    status = MirrorStatus("mirroring", "USB-XR", True, QUALITY_LEVELS[0], 58, None, "1920x1080")
    text = status_line(status, device="Galaxy XR", keys=True).plain
    assert text.startswith("Mirroring Galaxy XR · USB · ultra (1920px, 60 fps, 20 Mb/s) · 1920x1080 · 58 fps")
    assert text.endswith("+/- quality  r restart  q quit")
    measuring = MirrorStatus("measuring", "192.168.0.6:40001", False, QUALITY_LEVELS[0], delay_ms=420.0)
    text = status_line(measuring, device="XR", keys=False).plain
    assert text == "Measuring XR · network · queue 420 ms"


def test_automatic_selection_prefers_the_only_or_default_device(rig):
    manager, backend, discovery = rig
    console = Console(file=io.StringIO())
    backend.online["USB-A"] = connected("USB-A", "SERIAL-A")
    chosen = select_device(manager, console=console)
    assert chosen is not None and chosen.address == "USB-A"

    backend.online["USB-B"] = connected("USB-B", "SERIAL-B", "Tablet", "guid-b")
    with pytest.raises(DroidockError) as raised:
        select_device(manager, console=console)  # not a terminal: no list to choose from
    assert raised.value.code == "ambiguous_device"
    manager.register("USB-B", name="Tablet")
    chosen = select_device(manager, console=console)
    assert chosen is not None and chosen.address == "USB-B"


def test_automatic_selection_applies_device_criteria(rig):
    manager, backend, _ = rig
    backend.online["USB-A"] = connected("USB-A", "SERIAL-A", "Phone")
    backend.online["USB-XR"] = USB
    chosen = select_device(
        manager, console=Console(file=io.StringIO()), criteria=DeviceCriteria(models=("SM-I610",))
    )
    assert chosen is not None and chosen.address == "USB-XR"


def test_run_mirror_names_the_window_after_the_device(rig, monkeypatch):
    manager, backend, _ = rig
    backend.online["USB-XR"] = USB
    manager.register("USB-XR", name="Demo headset")
    captured: dict = {}

    class Session:
        def __init__(self, manager, transport, *, options, install, on_event, commands):
            captured["options"] = options
            captured["transport"] = transport
            on_event(type("Event", (), {"kind": "quality", "message": "USB: starting at ultra."})())

        @property
        def status(self):
            return MirrorStatus("mirroring", "USB-XR", True, QUALITY_LEVELS[0])

        def run(self):
            return MirrorReport("USB-XR", QUALITY_LEVELS[0], "closed", restarts=1)

    class Installer:
        release = type("Release", (), {"version": "5.0.1"})()

        def find(self):
            return INSTALL

    monkeypatch.setattr("droidock.mirror_view.MirrorSession", Session)
    output = io.StringIO()
    report = run_mirror(
        manager,
        options=MirrorOptions(title="Galaxy XR · {device}"),
        console=Console(file=output, width=120),
        installer=Installer(),  # type: ignore[arg-type]
    )
    assert report is not None and captured["options"].title == "Galaxy XR · Demo headset"
    assert captured["transport"].address == "USB-XR"
    rendered = output.getvalue()
    assert "USB: starting at ultra." in rendered
    assert "The mirror window was closed. (1 quality restart)" in rendered


def test_mirror_command_passes_options(rig):
    manager, _, _ = rig
    with (
        patch("droidock.cli.ConnectionManager", return_value=manager),
        patch("droidock.mirror_view.run_mirror") as run,
    ):
        result = runner.invoke(
            app,
            [
                "mirror",
                "Demo headset",
                "--view-only",
                "--quality",
                "Light",
                "--no-adapt",
                "--on-top",
                "--scrcpy-arg=--crop=1224:1440:0:0",
            ],
        )
    assert result.exit_code == 0, result.output
    (called_manager, device), kwargs = run.call_args
    assert called_manager is manager and device == "Demo headset"
    options = kwargs["options"]
    assert (options.quality, options.adaptive, options.control, options.always_on_top) == (
        "light",
        False,
        False,
        True,
    )
    assert options.scrcpy_arguments == ("--crop=1224:1440:0:0",)
    assert kwargs["pick"] is False


def test_mirror_command_rejects_unknown_qualities():
    result = runner.invoke(app, ["mirror", "--quality", "cinema"])
    assert result.exit_code == 1
    assert "Choose a quality: auto, ultra, high" in result.output


def test_scrcpy_command_reports_the_executable(tmp_path):
    with patch("droidock.cli.ScrcpyInstaller") as installer:
        installer.return_value.find.return_value = INSTALL
        result = runner.invoke(app, ["scrcpy", "--json"])
    assert result.exit_code == 0, result.output
    assert '"version": "5.0.1"' in result.output and '"source": "managed"' in result.output


def test_device_list_connects_nearby_and_saved_devices(rig, monkeypatch):
    manager, backend, discovery = rig
    backend.online["USB-A"] = connected("USB-A", "SERIAL-A", "Phone")
    backend.network["192.168.0.6:40001"] = NETWORK
    discovery.services = [Service("adb-SERIAL-XR-x", ServiceKind.CONNECT, "192.168.0.6:40001")]
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    picker = InteractiveCli(manager, plain=True, console=Console(file=io.StringIO(), width=100))
    chosen = picker.pick_device()
    assert chosen is not None and chosen.address == "192.168.0.6:40001"
    assert manager.store.read().devices == []

    answers = iter(["2"])  # connected devices are listed by address; the headset is now first
    chosen = picker.pick_device()
    assert chosen is not None and chosen.address == "USB-A"

    answers = iter(["0"])
    assert picker.pick_device() is None


XR_OUTPUT = (
    "Physical size: 7104x3840\r\n"
    "feature:android.hardware.vulkan.level=1\n"
    "feature:android.software.xr.api.openxr=65537\n"
    "feature:com.quicinc.voice.assist.uvr\n"
)


def test_headset_displays_are_recognised_from_size_and_features():
    from droidock.mirror import parse_display

    headset = parse_display(XR_OUTPUT)
    assert headset is not None and (headset.width, headset.height, headset.headset) == (7104, 3840, True)
    assert headset.eye_crop("left") == "3552:3840:0:0"
    assert headset.eye_crop("right") == "3552:3840:3552:0"
    assert headset.eye_crop("both") is None
    phone = parse_display("Physical size: 1080x2340\nfeature:android.hardware.camera\n")
    assert phone is not None and not phone.headset and phone.eye_crop("left") is None
    tablet = parse_display("Physical size: 2560x1600\nfeature:android.hardware.vulkan.level=1\n")
    assert tablet is not None and not tablet.headset
    wide_without_xr = parse_display("Physical size: 7104x3840\nfeature:com.quicinc.voice.assist.uvr\n")
    assert wide_without_xr is not None and not wide_without_xr.headset
    assert parse_display("error: no devices") is None


class CommandBackend:
    """A FakeBackend with the command capability, answering the display query."""

    def __init__(self, backend, output: str):
        self._backend = backend
        self.output = output
        self.commands: list[tuple[list[str], str | None]] = []

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def run(self, arguments, *, serial=None, input_text=None, timeout=None, cwd=None, check=True):
        from droidock import CommandResult

        self.commands.append((list(arguments), serial))
        return CommandResult(0, self.output, "")


def test_headsets_show_one_eye_without_forwarding_input(rig):
    manager, backend, _ = rig
    manager.backend = CommandBackend(backend, XR_OUTPUT)
    events: list = []
    launcher = Launcher(FakeProcess(121, polls=0))
    make_session(manager, NETWORK, launcher, events=events).run()
    arguments = launcher.calls[0][0]
    assert "--crop=3552:3840:0:0" in arguments and "--no-control" in arguments
    assert manager.backend.commands == [(["shell", "wm size; pm list features"], NETWORK.address)]
    assert [e.message for e in events if e.kind == "display"] == [
        "Headset display 7104x3840: mirroring the left eye, view only."
    ]


def test_explicit_eye_and_control_choices_override_the_headset_defaults(rig):
    manager, backend, _ = rig
    manager.backend = CommandBackend(backend, XR_OUTPUT)
    launcher = Launcher(FakeProcess(131, polls=0), FakeProcess(132, polls=0))
    make_session(manager, USB, launcher, options=MirrorOptions(eye="both", control=True)).run()
    make_session(manager, USB, launcher, options=MirrorOptions(eye="right")).run()
    both, right = (call[0] for call in launcher.calls)
    assert not any(a.startswith("--crop") for a in both) and "--no-control" not in both
    assert "--crop=3552:3840:3552:0" in right and "--no-control" in right


def test_phones_mirror_the_whole_screen_with_input(rig):
    manager, backend, _ = rig
    manager.backend = CommandBackend(backend, "Physical size: 1080x2340\n")
    events: list = []
    launcher = Launcher(FakeProcess(141, polls=0), FakeProcess(142, polls=0))
    make_session(manager, USB, launcher).run()
    make_session(manager, USB, launcher, events=events, options=MirrorOptions(eye="left")).run()
    automatic, forced = (call[0] for call in launcher.calls)
    assert not any(a.startswith("--crop") for a in automatic + forced)
    assert "--no-control" not in automatic and "--no-control" not in forced
    assert any(e.kind == "warning" and "side by side" in e.message for e in events)


def test_backends_without_commands_mirror_the_whole_display(rig):
    manager, _, _ = rig
    launcher = Launcher(FakeProcess(151, polls=0))
    make_session(manager, USB, launcher).run()
    arguments = launcher.calls[0][0]
    assert not any(a.startswith("--crop") for a in arguments) and "--no-control" not in arguments


def test_unknown_eyes_are_rejected():
    result = runner.invoke(app, ["mirror", "--eye", "middle"])
    assert result.exit_code == 1 and "Choose an eye: auto, left, right, both" in result.output


def test_monitor_ignores_slow_replies_while_no_video_flows():
    monitor = LinkMonitor(SilentProbe(), base_ms=10.0, settle_seconds=0.0, clock=Clock())  # type: ignore[arg-type]
    monitor.record(20.0)  # ends settling
    for _ in range(5):
        monitor.record(900.0, active=False)  # Wi-Fi power saving while the screen is off
    assert not monitor.congested and monitor.delay_ms == 890.0
    monitor.record(900.0)
    monitor.record(900.0)
    monitor.record(900.0, active=False)  # a pause in the video breaks the run
    monitor.record(900.0)
    monitor.record(900.0)
    assert not monitor.congested
    monitor.record(900.0)
    assert monitor.congested


def test_sessions_count_link_samples_only_while_frames_flow(rig, monkeypatch):
    manager, _, _ = rig
    checks: list = []

    class Recorder:
        def __init__(self, probe, *, active, **kwargs):
            checks.append(active)
            self.congested = False
            self.delay_ms = None
            self.recent_delay_ms = 0.0

        def start(self):
            pass

        def stop(self):
            pass

        def rearm(self):
            pass

    monkeypatch.setattr("droidock.mirror.LinkMonitor", Recorder)
    make_session(manager, NETWORK, Launcher(FakeProcess(161, ("INFO: 0 fps",), polls=0))).run()
    make_session(manager, NETWORK, Launcher(FakeProcess(162, ("INFO: 30 fps",), polls=0))).run()
    assert [check() for check in checks] == [False, True]
