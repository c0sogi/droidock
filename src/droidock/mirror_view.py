"""Terminal front end for mirroring: scrcpy setup, device choice, live status, and keys."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from types import TracebackType
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.text import Text

from .errors import DroidockError
from .manager import ConnectionManager
from .mirror import QUEUE_THRESHOLD_MS, MirrorEvent, MirrorOptions, MirrorReport, MirrorSession, MirrorStatus
from .models import DeviceCriteria, Transport
from .scrcpy import ScrcpyInstall, ScrcpyInstaller

KEY_COMMANDS = {"+": "up", "=": "up", "-": "down", "_": "down", "r": "restart", "q": "quit", "\x1b": "quit"}
EVENT_STYLES = {
    "display": "cyan",
    "quality": "cyan",
    "measure": "dim",
    "start": "green",
    "congestion": "yellow",
    "warning": "yellow",
    "scrcpy": "dim",
    "disconnected": "yellow",
    "reconnecting": "dim",
    "reconnected": "green",
}
STATE_LABELS = {
    "measuring": ("Measuring", "yellow"),
    "starting": ("Starting", "yellow"),
    "mirroring": ("Mirroring", "green"),
    "restarting": ("Restarting", "yellow"),
    "reconnecting": ("Reconnecting", "yellow"),
    "stopped": ("Stopped", "dim"),
}
# Errors that a device list cannot fix; anything else offers the list instead.
FATAL_SELECTION_CODES = {
    "invalid_store",
    "store_unwritable",
    "adb_missing",
    "server_conflict",
    "server_unavailable",
}


class KeyReader:
    """Single-key commands from the terminal without Enter; inactive when stdin is not a terminal."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stdin.isatty()
        self._saved: Any = None  # termios attributes, restored on exit

    def __enter__(self) -> KeyReader:
        if self.enabled and sys.platform != "win32":
            import termios
            import tty

            descriptor = sys.stdin.fileno()
            self._saved = termios.tcgetattr(descriptor)
            tty.setcbreak(descriptor)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._saved is not None and sys.platform != "win32":
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            self._saved = None

    def read(self) -> str | None:
        if not self.enabled:
            return None
        if sys.platform == "win32":
            import msvcrt

            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in {"\x00", "\xe0"}:
                    msvcrt.getwch()  # Arrow and function keys arrive as two characters.
                    continue
                command = KEY_COMMANDS.get(key.lower())
                if command:
                    return command
            return None
        import select

        while select.select([sys.stdin], [], [], 0)[0]:
            key = os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
            command = KEY_COMMANDS.get(key.lower())
            if command:
                return command
        return None


def status_line(status: MirrorStatus, *, device: str, keys: bool) -> Text:
    label, style = STATE_LABELS.get(status.state, (status.state.title(), ""))
    line = Text()
    line.append(f"{label} ", style=f"bold {style}")
    line.append(device, style="bold")
    line.append(" · USB" if status.wired else " · network")
    if status.state != "measuring":
        line.append(f" · {status.quality.label}")
    if status.video_size:
        line.append(f" · {status.video_size}")
    if status.fps is not None:
        line.append(f" · {status.fps} fps")
        if status.fps == 0 and status.state == "mirroring":
            line.append(" (screen idle or off)", style="dim")
    if status.delay_ms is not None:
        line.append(
            f" · queue {status.delay_ms:.0f} ms", style="red" if status.delay_ms >= QUEUE_THRESHOLD_MS else ""
        )
    if keys:
        line.append("   +/- quality  r restart  q quit", style="dim")
    return line


def prepare_scrcpy(console: Console, installer: ScrcpyInstaller) -> ScrcpyInstall:
    """Return scrcpy, downloading the pinned release with a progress bar the first time."""
    install = installer.find()
    if install is not None:
        return install
    console.print(
        f"Installing scrcpy {installer.release.version} for mirroring. This happens once; "
        "the download is checked against its published SHA-256 digest.",
        markup=False,
    )
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("scrcpy", total=None)
        install = installer.install(
            lambda received, total: progress.update(task, completed=received, total=total)
        )
    console.print(f"Installed scrcpy {install.version}: {install.executable}", style="green", markup=False)
    return install


def device_label(manager: ConnectionManager, transport: Transport) -> str:
    identity = transport.identity
    if identity is None:
        return transport.address
    record = next((d for d in manager.store.read().devices if d.serial == identity.serial), None)
    if record is not None:
        return record.name
    return f"{identity.model or 'Android'} {identity.serial[-4:]}"


def select_device(
    manager: ConnectionManager,
    device: str | Transport | None = None,
    *,
    console: Console,
    criteria: DeviceCriteria | None = None,
    pick: bool = False,
    plain: bool = False,
) -> Transport | None:
    """Use the named device, the only (or default) responding one, or a saved or nearby one.

    When that is ambiguous or fails in a terminal, offer a list instead. None means cancelled.
    """
    if isinstance(device, Transport):
        return device
    interactive = plain or sys.stdin.isatty()

    def choose() -> Transport | None:
        if not interactive:
            raise DroidockError(
                "Name the device to mirror, or run the command in a terminal to choose one.",
                code="ambiguous_device",
            )
        from .interactive import InteractiveCli

        return InteractiveCli(manager, plain=plain, console=console).pick_device(criteria=criteria)

    if pick:
        return choose()
    if device:
        with console.status("Connecting to the device..."):
            return manager.ensure_connected(device, criteria=criteria)
    with console.status("Looking for connected devices..."):
        groups = [
            group
            for group in manager.device_groups(criteria=criteria)
            if not group.conflict and group.transports[0].ready
        ]
    if len(groups) == 1:
        return groups[0].transports[0]
    if groups:
        state = manager.store.read()
        default = next((d for d in state.devices if d.id == state.default_device), None)
        for group in groups:
            if default and any(t.identity and default.matches(t.identity) for t in group.transports):
                return group.transports[0]
        return choose()
    try:
        with console.status("Searching for saved and nearby devices..."):
            return manager.ensure_connected(None, criteria=criteria, attempts=1)
    except DroidockError as exc:
        if not interactive or exc.code in FATAL_SELECTION_CODES:
            raise
        console.print(str(exc).splitlines()[0], style="yellow", markup=False)
        return choose()


def run_mirror(
    manager: ConnectionManager,
    device: str | Transport | None = None,
    *,
    options: MirrorOptions | None = None,
    console: Console | None = None,
    criteria: DeviceCriteria | None = None,
    pick: bool = False,
    plain: bool = False,
    installer: ScrcpyInstaller | None = None,
) -> MirrorReport | None:
    """Prepare scrcpy, choose the device, and mirror it with a live status line.

    The window title may contain {device}, replaced by the saved name or model.
    Returns None when the device choice was cancelled or the user pressed Ctrl+C.
    """
    console = console or Console()
    options = options or MirrorOptions()
    install = prepare_scrcpy(console, installer or ScrcpyInstaller())
    transport = select_device(manager, device, console=console, criteria=criteria, pick=pick, plain=plain)
    if transport is None:
        return None
    label = device_label(manager, transport)
    options = replace(options, title=(options.title or "{device}").replace("{device}", label))
    console.print(
        f"scrcpy {install.version} ({install.source}): {install.executable}", style="dim", markup=False
    )

    def show(event: MirrorEvent) -> None:
        prefix = "scrcpy: " if event.kind == "scrcpy" else ""
        console.print(
            prefix + event.message, style=EVENT_STYLES.get(event.kind, ""), markup=False, highlight=False
        )

    try:
        with KeyReader() as keys:
            session = MirrorSession(
                manager, transport, options=options, install=install, on_event=show, commands=keys.read
            )
            with Live(
                get_renderable=lambda: status_line(session.status, device=label, keys=keys.enabled),
                console=console,
                refresh_per_second=4,
                transient=True,
            ):
                report = session.run()
    except KeyboardInterrupt:
        console.print("Mirroring stopped.", style="dim")
        return None
    endings = {
        "closed": "The mirror window was closed.",
        "quit": "Mirroring stopped.",
        "disconnected": "The device disconnected.",
    }
    details = []
    if report.restarts:
        details.append(f"{report.restarts} quality restart{'s' if report.restarts != 1 else ''}")
    if report.reconnects:
        details.append(f"{report.reconnects} reconnection{'s' if report.reconnects != 1 else ''}")
    suffix = f" ({', '.join(details)})" if details else ""
    console.print(endings.get(report.reason, "Mirroring ended.") + suffix, style="dim", markup=False)
    return report
