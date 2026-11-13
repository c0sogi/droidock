"""Low-latency screen mirroring through scrcpy, sized to the measured ADB link.

The video shares the device's ADB transport with every other command. When the link carries
less than the encoder produces, the surplus waits in socket buffers and the picture falls
further behind, and scrcpy cannot change its bit rate while running. A session therefore sizes
the stream from a short measurement before starting, samples the queueing delay with tiny
shell round trips on the same transport while video flows, and restarts scrcpy at a lower
quality when that delay keeps growing. USB connections start at the highest quality.
"""

from __future__ import annotations

import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import IO, Literal, Protocol

from ._windows import WindowKeeper, WindowPlacement, window_keeper
from .adb import AdbBackend
from .errors import DroidockError
from .manager import ConnectionManager
from .models import Transport
from .scrcpy import ScrcpyInstall, ScrcpyInstaller


@dataclass(frozen=True)
class MirrorQuality:
    """One step of the quality ladder: limits for the device's video encoder."""

    name: str
    max_size: int
    max_fps: int
    bit_rate: int

    @property
    def label(self) -> str:
        return f"{self.name} ({self.max_size}px, {self.max_fps} fps, {self.bit_rate / 1_000_000:g} Mb/s)"


# Ordered from highest to lowest. Each step needs roughly 60% of the previous bit rate.
QUALITY_LEVELS: tuple[MirrorQuality, ...] = (
    MirrorQuality("ultra", 1920, 60, 20_000_000),
    MirrorQuality("high", 1600, 60, 12_000_000),
    MirrorQuality("sharp", 1280, 60, 8_000_000),
    MirrorQuality("balanced", 1280, 30, 5_000_000),
    MirrorQuality("light", 1024, 30, 3_000_000),
    MirrorQuality("low", 800, 30, 1_800_000),
    MirrorQuality("minimal", 640, 24, 1_000_000),
)
# A network link starts at most at "high" and uses 40% of its measured throughput, leaving
# room for the encoder's bursts and other traffic. Without a measurement it starts "balanced".
WIRELESS_SHARE = 0.4
WIRELESS_CEILING = 1
WIRELESS_FALLBACK = 3
SLOW_ROUND_TRIP_MS = 80.0

QUEUE_THRESHOLD_MS = 350.0
SEVERE_QUEUE_MS = 1000.0
STARTUP_SECONDS = 10.0
POLL_SECONDS = 0.1


def quality_index(value: str | int) -> int:
    """Index of a quality name ("balanced") or ladder position (0 is the highest)."""
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value < len(QUALITY_LEVELS):
            return value
    elif isinstance(value, str):
        for index, level in enumerate(QUALITY_LEVELS):
            if level.name == value.strip().casefold():
                return index
    raise DroidockError(
        "Choose a quality: auto, " + ", ".join(level.name for level in QUALITY_LEVELS) + ".",
        code="invalid_quality",
    )


@dataclass(frozen=True)
class LinkMeasurement:
    wired: bool
    throughput_bps: float | None = None
    round_trip_ms: float | None = None

    @property
    def summary(self) -> str:
        parts = ["USB" if self.wired else "Network"]
        if self.throughput_bps is not None:
            parts.append(f"{self.throughput_bps / 1_000_000:.1f} Mb/s")
        if self.round_trip_ms is not None:
            parts.append(f"round trip {self.round_trip_ms:.0f} ms")
        return ", ".join(parts)


def choose_quality(link: LinkMeasurement) -> int:
    """The starting ladder index for a measured link."""
    if link.wired:
        return 0
    if link.throughput_bps is None:
        index = WIRELESS_FALLBACK
    else:
        budget = link.throughput_bps * WIRELESS_SHARE
        index = next(
            (i for i, level in enumerate(QUALITY_LEVELS) if level.bit_rate <= budget),
            len(QUALITY_LEVELS) - 1,
        )
        index = max(index, WIRELESS_CEILING)
    if link.round_trip_ms is not None and link.round_trip_ms > SLOW_ROUND_TRIP_MS:
        # Long round trips (VPNs, distant routes) recover slowly from bursts.
        index = max(index, WIRELESS_FALLBACK)
    return index


def _receive(connection: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise DroidockError("The ADB server closed the connection.", code="connection_error")
        data += chunk
    return data


class AdbLinkProbe:
    """Time short requests to one device through the local ADB server, without extra processes."""

    BLOCK = 64 * 1024
    STAGES = (128 * 1024, 1024 * 1024, 8 * 1024 * 1024)

    def __init__(self, serial: str, *, port: int = 5037, host: str = "127.0.0.1") -> None:
        self.serial = serial
        self.port = port
        self.host = host

    def _open(self, service: str, timeout: float) -> socket.socket:
        connection = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            for request in (f"host:transport:{self.serial}", service):
                payload = request.encode("utf-8")
                connection.sendall(f"{len(payload):04x}".encode("ascii") + payload)
                status = _receive(connection, 4)
                if status == b"FAIL":
                    length = int(_receive(connection, 4), 16)
                    message = _receive(connection, length).decode("utf-8", "replace")
                    raise DroidockError(f"ADB refused the request: {message}", code="adb_failed")
                if status != b"OKAY":
                    raise DroidockError("The ADB server sent an unexpected response.", code="adb_failed")
        except BaseException:
            connection.close()
            raise
        return connection

    def round_trip(self, timeout: float = 2.5) -> float:
        """Seconds for a shell echo. Video queued on the transport delays the reply too."""
        started = time.perf_counter()
        try:
            with self._open("shell:echo droidock", timeout) as connection:
                while connection.recv(4096):
                    if time.perf_counter() - started > timeout:
                        raise TimeoutError
        except TimeoutError as exc:
            raise DroidockError("The device did not answer in time.", code="timeout") from exc
        except OSError as exc:
            raise DroidockError(
                f"Cannot reach the device through ADB: {exc}", code="connection_error"
            ) from exc
        return time.perf_counter() - started

    def throughput(self, *, budget: float = 2.0) -> float | None:
        """Bits per second the device sends through ADB, from transfers of growing size.

        Each transfer is read to the end so nothing is left queued ahead of the video.
        """
        deadline = time.perf_counter() + budget
        rate: float | None = None
        for size in self.STAGES:
            remaining = deadline - time.perf_counter()
            if remaining <= 0.1:
                break
            try:
                measured, elapsed = self._transfer(size, timeout=max(remaining, 4.0))
            except (DroidockError, OSError):
                break
            rate = measured or rate
            if elapsed >= 0.2:
                break
        return rate

    def _transfer(self, size: int, *, timeout: float) -> tuple[float | None, float]:
        blocks = max(1, size // self.BLOCK)
        command = f"exec:dd if=/dev/zero bs={self.BLOCK} count={blocks} 2>/dev/null"
        started = time.perf_counter()
        first = last = 0.0
        after_first = 0
        with self._open(command, timeout) as connection:
            while True:
                chunk = connection.recv(1 << 16)
                if not chunk:
                    break
                now = time.perf_counter()
                if not first:
                    first = now
                else:
                    after_first += len(chunk)
                last = now
                if now - started > timeout:
                    break
        elapsed = time.perf_counter() - started
        if not first or after_first == 0 or last - first < 0.005:
            return None, elapsed
        return after_first * 8 / (last - first), elapsed


class LinkMonitor:
    """Sample a device's ADB round trip while video streams over the same transport.

    The delay above the fastest observed round trip is the time video waits in buffers.
    Several consecutive samples over the threshold mark the link as congested. After a start
    or rearm, sampling waits until earlier queued data has drained (or settle_seconds pass).

    Only samples taken while active() is true count. With no video flowing (a headset that is
    not worn, a screen that is off), Wi-Fi power saving alone can delay replies by most of a
    second, and that must not lower the quality.
    """

    def __init__(
        self,
        probe: AdbLinkProbe,
        *,
        base_ms: float | None = None,
        interval: float = 1.0,
        timeout: float = 2.5,
        threshold_ms: float = QUEUE_THRESHOLD_MS,
        required: int = 3,
        settle_seconds: float = 12.0,
        active: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._active = active
        self.base_ms = base_ms
        self.interval = interval
        self.timeout = timeout
        self.threshold_ms = threshold_ms
        self.required = required
        self.settle_seconds = settle_seconds
        self._clock = clock
        self.delay_ms: float | None = None
        self._recent: deque[float] = deque(maxlen=required)
        self._streak = 0
        self._settling_since: float | None = clock()
        self._lock = threading.Lock()
        self._congested = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def congested(self) -> bool:
        return self._congested.is_set()

    @property
    def recent_delay_ms(self) -> float:
        with self._lock:
            return statistics.median(self._recent) if self._recent else 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="droidock-link-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)

    def rearm(self) -> None:
        with self._lock:
            self._congested.clear()
            self._streak = 0
            self._recent.clear()
            self._settling_since = self._clock()

    def record(self, round_trip_ms: float | None, *, active: bool = True) -> None:
        """Account one round trip in milliseconds, or None when it timed out.

        An inactive sample is shown but breaks any run of slow samples.
        """
        with self._lock:
            if round_trip_ms is not None and (self.base_ms is None or round_trip_ms < self.base_ms):
                self.base_ms = round_trip_ms
            if round_trip_ms is None:
                delay = self.timeout * 1000
            else:
                delay = max(
                    0.0, round_trip_ms - (self.base_ms if self.base_ms is not None else round_trip_ms)
                )
            self.delay_ms = delay
            if not active:
                self._streak = 0
                self._recent.clear()
                return
            if self._settling_since is not None:
                drained = delay < self.threshold_ms / 2
                if drained or self._clock() - self._settling_since >= self.settle_seconds:
                    self._settling_since = None
                return
            self._recent.append(delay)
            self._streak = self._streak + 1 if delay >= self.threshold_ms else 0
            if self._streak >= self.required:
                self._congested.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sample: float | None = self._probe.round_trip(self.timeout) * 1000
            except DroidockError:
                sample = None
            if self._stop.is_set():
                return
            self.record(sample, active=self._active() if self._active is not None else True)
            self._stop.wait(self.interval)


EYES = ("both", "left", "right")
HEADSET_ASPECT = 1.8
_XR_FEATURE = re.compile(r"\.(?:xr|vr)\.", re.IGNORECASE)


@dataclass(frozen=True)
class DisplayInfo:
    """The main display in its natural orientation, and whether it is a headset's eye panel."""

    width: int
    height: int
    headset: bool = False

    def eye_crop(self, eye: str) -> str | None:
        """The scrcpy crop for one half of a side-by-side display, or None."""
        if eye not in {"left", "right"} or self.width <= self.height:
            return None
        half = self.width // 2
        return f"{half}:{self.height}:{0 if eye == 'left' else half}:0"


def parse_display(output: str) -> DisplayInfo | None:
    """Read ``wm size`` and ``pm list features`` output.

    A headset drives both eyes from one wide panel (Galaxy XR: 7104x3840) and declares XR or VR
    system features; phones, tablets, and TVs are narrower or declare neither.
    """
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    features = re.findall(r"^feature:([^\s=]+)", output, flags=re.MULTILINE)
    headset = width >= height * HEADSET_ASPECT and any(_XR_FEATURE.search(name + ".") for name in features)
    return DisplayInfo(width, height, headset)


@dataclass(frozen=True)
class MirrorOptions:
    """Choices for a mirroring session.

    quality=None measures the link and chooses. control=None forwards input except to headsets.
    eye="both" mirrors the whole display. "left" or "right" crops one half of a headset whose
    mirror shows both eye panels side by side; scrcpy then renders the full display and crops
    every frame on the device. Galaxy XR mirrors one undistorted view, which needs no crop.
    """

    quality: str | int | None = None
    adaptive: bool = True
    control: bool | None = None
    eye: str = "both"
    audio: bool = False
    title: str = ""
    fullscreen: bool = False
    always_on_top: bool = False
    reconnect: bool = True
    scrcpy_arguments: tuple[str, ...] = ()


def scrcpy_arguments(
    serial: str,
    quality: MirrorQuality,
    options: MirrorOptions,
    *,
    install: ScrcpyInstall | None = None,
    placement: WindowPlacement | None = None,
    crop: str | None = None,
) -> list[str]:
    """scrcpy arguments for a latency-first stream; options the install lacks are left out."""
    arguments = [
        f"--serial={serial}",
        # H.264 has the shortest encoder pipelines; scrcpy already requests real-time
        # priority and one frame of encoder latency, and buffers no video by default.
        "--video-codec=h264",
        f"--max-size={quality.max_size}",
        f"--max-fps={quality.max_fps}",
        f"--video-bit-rate={quality.bit_rate}",
        "--print-fps",
        "--disable-screensaver",
        "--no-terminal-title",
    ]
    if not options.audio:
        arguments.append("--no-audio")
    if options.control is False:
        arguments.append("--no-control")
    if crop:
        arguments.append(f"--crop={crop}")
    if options.title:
        arguments.append(f"--window-title={options.title}")
    if options.always_on_top:
        arguments.append("--always-on-top")
    if placement is not None and placement.fullscreen:
        # A borderless window covering the monitor, placed after it appears.
        arguments += ["--window-borderless", "--no-window-aspect-ratio-lock"]
    elif options.fullscreen and placement is None:
        arguments.append("--fullscreen")
    if install is not None:
        arguments = [a for a in arguments if install.supports(a.split("=", 1)[0])]
    return arguments + list(options.scrcpy_arguments)


class ScrcpyProcess(Protocol):
    pid: int
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


Launcher = Callable[[Sequence[str], dict[str, str]], ScrcpyProcess]


def launch_scrcpy(arguments: Sequence[str], environment: dict[str, str]) -> ScrcpyProcess:
    """Start scrcpy in its own process group so it can be stopped cleanly."""
    if sys.platform == "win32":
        return subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )


def stop_scrcpy(process: ScrcpyProcess, timeout: float = 5.0) -> None:
    """Ask scrcpy to quit so it removes its ADB tunnel; kill it if it does not."""
    if process.poll() is not None:
        return
    try:
        # scrcpy handles Ctrl+Break on Windows and SIGTERM elsewhere like closing its window.
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGTERM)
    except (OSError, ValueError):
        process.kill()
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout)


@dataclass(frozen=True)
class MirrorEvent:
    """kind: display, measure, quality, start, congestion, warning, scrcpy, disconnected, reconnecting,
    or reconnected."""

    kind: str
    message: str


@dataclass(frozen=True)
class MirrorStatus:
    state: str
    address: str
    wired: bool
    quality: MirrorQuality
    fps: int | None = None
    delay_ms: float | None = None
    video_size: str = ""


@dataclass
class MirrorReport:
    address: str
    quality: MirrorQuality
    reason: str = ""
    restarts: int = 0
    reconnects: int = 0


@dataclass(frozen=True)
class _Outcome:
    kind: Literal["closed", "quit", "restart", "disconnected", "failed"]
    placement: WindowPlacement | None = None


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FPS = re.compile(r"\b(\d+) fps\b")
_TEXTURE = re.compile(r"\bTexture(?: \([^)]*\))?: (\d+x\d+)")  # "Texture (D3D11VA): 1280x704" on the GPU
_PROBLEM = re.compile(r"^(?:\[server\]\s*)?(ERROR|WARN):\s*(.*)$")


class MirrorSession:
    """Mirror one verified device until its window is closed or the caller quits.

    No terminal output: progress is reported through on_event and the status property.
    commands() may return "up", "down", "restart", or "quit" and is polled while mirroring.
    """

    def __init__(
        self,
        manager: ConnectionManager,
        transport: Transport,
        *,
        options: MirrorOptions | None = None,
        install: ScrcpyInstall | None = None,
        installer: ScrcpyInstaller | None = None,
        on_event: Callable[[MirrorEvent], None] | None = None,
        commands: Callable[[], str | None] | None = None,
        launcher: Launcher | None = None,
        windows: WindowKeeper | Literal["auto"] | None = "auto",
        probe_factory: Callable[[str, int], AdbLinkProbe] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not transport.ready:
            raise DroidockError("Mirroring requires a verified device connection.", code="unverified_device")
        self.manager = manager
        self.transport = transport
        self.options = options or MirrorOptions()
        if self.options.quality is not None:
            quality_index(self.options.quality)
        if self.options.eye not in EYES:
            raise DroidockError("Choose an eye: " + ", ".join(EYES) + ".", code="invalid_eye")
        self._install = install
        self._installer = installer
        self._on_event = on_event
        self._commands = commands
        self._launcher = launcher or launch_scrcpy
        self._windows = window_keeper() if windows == "auto" else windows
        self._probe_factory = probe_factory or (lambda serial, port: AdbLinkProbe(serial, port=port))
        self._sleep = sleep
        self._clock = clock
        self._port = manager.settings.server_port
        self._index = quality_index(self.options.quality) if self.options.quality is not None else 0
        self._base_ms: float | None = None
        self._lock = threading.Lock()
        self._state = "measuring"
        self._fps: int | None = None
        self._video_size = ""
        self._streaming = False
        self._errors: deque[str] = deque(maxlen=12)
        self._process: ScrcpyProcess | None = None
        self._reader: threading.Thread | None = None
        self._monitor: LinkMonitor | None = None
        self.display: DisplayInfo | None = None
        self._crop: str | None = None
        self._view = self.options

    @property
    def quality(self) -> MirrorQuality:
        return QUALITY_LEVELS[self._index]

    @property
    def status(self) -> MirrorStatus:
        with self._lock:
            monitor = self._monitor
            return MirrorStatus(
                self._state,
                self.transport.address,
                not self.transport.wireless,
                QUALITY_LEVELS[self._index],
                self._fps,
                monitor.delay_ms if monitor is not None else None,
                self._video_size,
            )

    def run(self) -> MirrorReport:
        if self._install is None:
            self._install = (self._installer or ScrcpyInstaller()).ensure()
        self._choose_view()
        link = self._measure()
        if self.options.quality is None:
            self._index = choose_quality(link)
        self._event("quality", f"{link.summary}: starting at {self.quality.label}.")
        report = MirrorReport(self.transport.address, self.quality)
        placement: WindowPlacement | None = None
        quick_failures = 0
        try:
            while True:
                started = self._clock()
                process = self._start(placement)
                outcome = self._watch(process, started)
                placement = outcome.placement
                if outcome.kind in {"closed", "quit"}:
                    report.reason = outcome.kind
                    break
                if outcome.kind == "restart":
                    report.restarts += 1
                    continue
                if outcome.kind == "failed":
                    quick_failures += 1
                    if not report.restarts and not report.reconnects or quick_failures >= 3:
                        raise DroidockError(self._failure_message(), code="mirror_failed")
                else:
                    quick_failures = 0
                if not self.options.reconnect:
                    report.reason = "disconnected"
                    break
                if not self._reconnect():
                    report.reason = "quit"
                    break
                report.reconnects += 1
        finally:
            self._stop_current()
            self._set_state("stopped")
        report.address = self.transport.address
        report.quality = self.quality
        return report

    def _event(self, kind: str, message: str) -> None:
        if self._on_event is not None:
            self._on_event(MirrorEvent(kind, message))

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def _choose_view(self) -> None:
        """Mirror the whole display, and forward no input to a headset unless the options say so."""
        try:
            result = self.manager.run(
                ["shell", "wm size; pm list features"], device=self.transport, timeout=10, check=False
            )
            self.display = parse_display(result.stdout)
        except DroidockError:
            self.display = None
        headset = self.display is not None and self.display.headset
        eye = self.options.eye
        self._crop = self.display.eye_crop(eye) if self.display is not None else None
        control = self.options.control if self.options.control is not None else not headset
        self._view = replace(self.options, control=control)
        if eye in {"left", "right"} and self._crop is None:
            self._event(
                "warning", "The display does not show two eyes side by side, so all of it is mirrored."
            )
        elif self.display is not None and (headset or self._crop is not None):
            kind = "Headset display" if headset else "Display"
            view = f"the {eye} eye" if self._crop is not None else "the whole display"
            input_note = "" if control else ", view only"
            self._event(
                "display",
                f"{kind} {self.display.width}x{self.display.height}: mirroring {view}{input_note}.",
            )

    def _measure(self) -> LinkMeasurement:
        probe = self._probe_factory(self.transport.address, self._port)
        samples: list[float] = []
        for _ in range(3):
            try:
                samples.append(probe.round_trip(2.0) * 1000)
            except DroidockError:
                break
        self._base_ms = min(samples) if samples else None
        wired = not self.transport.wireless
        throughput = None
        if not wired and self.options.quality is None:
            self._event("measure", "Measuring the network link to the device...")
            throughput = probe.throughput()
        return LinkMeasurement(wired, throughput, self._base_ms)

    def _environment(self) -> dict[str, str]:
        environment = AdbBackend.environment()
        backend = self.manager.backend
        if isinstance(backend, AdbBackend):
            # One ADB build for every client of the server; scrcpy would otherwise use its own.
            environment["ADB"] = str(backend.executable)
        environment["ANDROID_ADB_SERVER_PORT"] = str(self._port)
        environment.pop("ANDROID_SERIAL", None)
        return environment

    def _start(self, placement: WindowPlacement | None) -> ScrcpyProcess:
        assert self._install is not None
        arguments = [
            str(self._install.executable),
            *scrcpy_arguments(
                self.transport.address,
                self.quality,
                self._view,
                install=self._install,
                placement=placement,
                crop=self._crop,
            ),
        ]
        with self._lock:
            self._fps = None
            self._video_size = ""
            self._streaming = False
            self._state = "starting"
        self._errors.clear()
        process = self._launcher(arguments, self._environment())
        self._process = process
        self._reader = threading.Thread(
            target=self._read_output, args=(process,), name="droidock-scrcpy-output", daemon=True
        )
        self._reader.start()
        if placement is not None and self._windows is not None:
            threading.Thread(
                target=self._windows.restore,
                args=(process.pid, placement),
                name="droidock-window",
                daemon=True,
            ).start()
        if self.options.adaptive and self.transport.wireless:
            monitor = LinkMonitor(
                self._probe_factory(self.transport.address, self._port),
                base_ms=self._base_ms,
                active=self._frames_flowing,
                clock=self._clock,
            )
            with self._lock:
                self._monitor = monitor
            monitor.start()
        self._event("start", f"Mirroring {self.transport.address} at {self.quality.label}.")
        return process

    def _frames_flowing(self) -> bool:
        with self._lock:
            return bool(self._fps)

    def _read_output(self, process: ScrcpyProcess) -> None:
        if process.stdout is None:
            return
        for raw in iter(process.stdout.readline, b""):
            line = _ANSI.sub("", raw.decode("utf-8", "replace")).strip()
            problem = _PROBLEM.match(line)
            if problem:
                self._errors.append(problem.group(2))
                self._event("scrcpy", line)
                continue
            fps = _FPS.search(line)
            texture = _TEXTURE.search(line)
            if fps or texture:
                with self._lock:
                    if fps:
                        self._fps = int(fps.group(1))
                    if texture:
                        self._video_size = texture.group(1)
                    self._streaming = True
                    self._state = "mirroring"

    def _stop_current(self) -> None:
        with self._lock:
            monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()
        process, self._process = self._process, None
        if process is not None:
            stop_scrcpy(process)
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None

    def _capture(self, process: ScrcpyProcess) -> WindowPlacement | None:
        return self._windows.capture(process.pid) if self._windows is not None else None

    def _restart(self, process: ScrcpyProcess, index: int, message: str, kind: str) -> _Outcome:
        placement = self._capture(process)
        self._set_state("restarting")
        self._stop_current()
        self._index = index
        self._event(kind, message)
        return _Outcome("restart", placement)

    def _watch(self, process: ScrcpyProcess, started: float) -> _Outcome:
        lowest = len(QUALITY_LEVELS) - 1
        warned = False
        while True:
            code = process.poll()
            if code is not None:
                self._stop_current()  # Also waits for the output reader to finish.
                if code == 0:
                    return _Outcome("closed")
                if code == 2 or self._streaming or self._clock() - started >= STARTUP_SECONDS:
                    return _Outcome("disconnected")
                return _Outcome("failed")
            command = self._commands() if self._commands is not None else None
            if command == "quit":
                self._stop_current()
                return _Outcome("quit")
            if command in {"up", "down", "restart"}:
                step = {"up": -1, "down": 1, "restart": 0}[command]
                target = min(max(self._index + step, 0), lowest)
                if command == "restart" or target != self._index:
                    level = QUALITY_LEVELS[target]
                    return self._restart(process, target, f"Restarting at {level.label}.", "quality")
            monitor = self._monitor
            if monitor is not None and monitor.congested:
                delay = monitor.recent_delay_ms
                if self._index < lowest:
                    target = min(lowest, self._index + (2 if delay >= SEVERE_QUEUE_MS else 1))
                    message = (
                        f"Video is waiting about {delay:.0f} ms on the link. "
                        f"Restarting at {QUALITY_LEVELS[target].label}."
                    )
                    return self._restart(process, target, message, "congestion")
                if not warned:
                    self._event(
                        "warning",
                        "The link still queues video at the lowest quality. Use USB or a less busy network.",
                    )
                    warned = True
                monitor.rearm()
            self._sleep(POLL_SECONDS)

    def _wait(self, seconds: float) -> bool:
        """Sleep while watching for quit; False when the caller quit."""
        deadline = self._clock() + seconds
        while self._clock() < deadline:
            if self._commands is not None and self._commands() == "quit":
                return False
            self._sleep(POLL_SECONDS)
        return True

    def _reconnect(self) -> bool:
        identity = self.transport.identity
        selector = identity.serial if identity is not None else self.transport.address
        self._set_state("reconnecting")
        self._event("disconnected", "The device disconnected. Reconnecting until it returns.")
        attempt = 0
        while True:
            if self._commands is not None and self._commands() == "quit":
                return False
            try:
                transport = self.manager.ensure_connected(selector, remember=False, attempts=1)
            except DroidockError as exc:
                attempt += 1
                if attempt in {1, 5} or attempt % 20 == 0:
                    self._event("reconnecting", str(exc).splitlines()[0])
                if not self._wait(min(1.0 + attempt, 5.0)):
                    return False
                continue
            if transport.ready:
                self.transport = transport
                self._event("reconnected", f"Reconnected through {transport.address}.")
                return True

    def _failure_message(self) -> str:
        detail = "\n".join(self._errors) or "scrcpy exited before showing video."
        return f"scrcpy could not mirror {self.transport.address}.\n{detail}"
