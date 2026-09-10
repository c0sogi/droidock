"""Human-readable terminal output shared by commands and interactive menus."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

from .models import (
    AutoConnectReport,
    DeviceRecord,
    Identity,
    PairResult,
    Service,
    ServiceGroup,
    ServiceKind,
    Settings,
    Snapshot,
    Transport,
    TransportGroup,
    endpoint_or_none,
)
from .portscan import PortScanProgress, PortScanStatus
from .selection import group_transports
from .tailscale import TailscalePeer


def show_tailscale_peers(console: Console, peers: list[TailscalePeer]) -> None:
    table = _table("Tailscale devices", "Device", "OS", "Status", "Addresses")
    for peer in peers:
        table.add_row(
            Text(peer.name),
            Text(peer.os),
            Text("Online" if peer.online else "Offline", style="green" if peer.online else "dim"),
            Text("\n".join(peer.addresses)),
        )
    if peers:
        console.print(table)
    else:
        console.print(
            "No Tailscale peers are available. Connect the device to this tailnet and refresh the list."
        )


def _duration(seconds: float) -> str:
    minutes, seconds_left = divmod(math.ceil(seconds), 60)
    return f"{minutes:02d}:{seconds_left:02d}"


class AdbScanDisplay:
    """Render core progress events; the scanner itself never imports Rich."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._next_plain_report = 0.0
        self.progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("{task.completed:.0f}/{task.total:.0f}"),
            TextColumn("Elapsed {task.fields[elapsed]}"),
            TextColumn("ETA {task.fields[eta]}"),
            console=console,
            auto_refresh=False,
        )
        self.task = self.progress.add_task("Finding ADB", total=1, elapsed="00:00", eta="estimating...")

    def update(self, event: PortScanProgress) -> None:
        descriptions = {
            PortScanStatus.RUNNING: "Finding ADB",
            PortScanStatus.FOUND: "ADB found",
            PortScanStatus.NOT_FOUND: "No ADB reply",
            PortScanStatus.TIMED_OUT: "Time limit reached",
            PortScanStatus.CANCELLED: "Cancelled",
        }
        eta = event.eta_seconds
        self.progress.update(
            self.task,
            description=descriptions[event.status],
            completed=event.completed,
            total=event.total,
            elapsed=_duration(event.elapsed_seconds),
            eta=("estimating..." if eta is None else _duration(eta))
            if event.status == PortScanStatus.RUNNING
            else "--",
            refresh=True,
        )
        if (
            not self.console.is_interactive
            and event.status == PortScanStatus.RUNNING
            and event.elapsed_seconds >= self._next_plain_report
        ):
            self.console.print(self.progress.get_renderable())
            self._next_plain_report = event.elapsed_seconds + 5


def _table(title: str, *columns: str) -> Table:
    table = Table(
        title=Text(title, style="bold cyan"),
        title_justify="left",
        box=box.ROUNDED,
        border_style="dim",
        header_style="bold",
        show_header=bool(columns),
        show_lines=True,
    )
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def _fields(console: Console, title: str, rows: Iterable[tuple[str, str | Text]]) -> None:
    table = _table(title)
    table.show_lines = False
    table.add_column(style="bold", overflow="fold")
    table.add_column(overflow="fold")
    for label, value in rows:
        table.add_row(Text(label), value if isinstance(value, Text) else Text(value))
    console.print(table)


def _enabled(value: bool) -> Text:
    return Text("Enabled" if value else "Disabled", style="green" if value else "dim")


def _last_seen(value: str) -> str:
    if not value:
        return "Never"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return value


def show_device(
    console: Console,
    record: DeviceRecord,
    *,
    title: str = "Device details",
    default_device: str | None = None,
) -> None:
    _fields(
        console,
        title,
        [
            ("Name", record.name),
            ("Manufacturer", record.manufacturer or "Not recorded"),
            ("Model", record.model or "Not recorded"),
            ("Serial number", record.serial),
            ("Default device", "Yes" if record.id == default_device else "No"),
            ("Automatic connection", _enabled(record.auto_connect)),
            ("Last seen (local time)", _last_seen(record.last_seen)),
            ("Saved wireless addresses", "\n".join(record.endpoints) or "None recorded"),
            ("Wireless identifiers", "\n".join(record.wifi_guids) or "None recorded"),
            ("Profile ID", record.id),
        ],
    )


def show_settings(console: Console, settings: Settings, path: Path) -> None:
    _fields(
        console,
        "Connection settings",
        [
            ("Connect on startup", _enabled(settings.auto_connect_on_start)),
            ("Wireless discovery", f"{settings.discovery_seconds:g} seconds"),
            ("Command timeout", f"{settings.command_timeout:g} seconds"),
            ("Connection attempts", str(settings.connect_attempts)),
            ("Local ADB server port", str(settings.server_port)),
            ("ADB executable", settings.adb_path or "Automatic (environment override or bundled ADB)"),
            ("Profile storage", str(path)),
        ],
    )


def show_pairing(console: Console, result: PairResult) -> None:
    rows = [("Pairing address", result.endpoint)]
    if result.guid:
        rows.append(("Pairing identifier", result.guid))
    _fields(console, "Pairing completed", rows)
    console.print("Next, use the current connection address from the device's Wireless debugging screen.")
    console.print("droidock connect --endpoint IP:PORT", style="cyan")
    console.print("The connection port differs from the pairing port.", style="dim")


def show_auto_connect(console: Console, report: AutoConnectReport, devices: list[DeviceRecord]) -> None:
    if not report.connected and not report.errors:
        console.print("No saved devices have automatic connection enabled.")
        return
    names = {device.id: device.name for device in devices}
    table = _table("Automatic connection results", "Device", "Result", "Details")
    for identifier in report.connected:
        table.add_row(
            Text(names.get(identifier, identifier)),
            Text("Connected", style="green"),
            Text("Connection verified."),
        )
    for identifier, message in report.errors.items():
        table.add_row(Text(names.get(identifier, identifier)), Text("Failed", style="red"), Text(message))
    console.print(table)


def show_disconnected(console: Console, count: int) -> None:
    if count:
        noun = "connection" if count == 1 else "connections"
        console.print(f"Disconnected {count} wireless {noun}.", style="green")
    else:
        console.print("No verified wireless connections were active.")
    console.print("Automatic connection is disabled for this device. Unplug the cable to disconnect USB.")


def _connection_status(transports: Iterable[Transport]) -> Text:
    connections = list(transports)
    if any(t.ready for t in connections):
        return Text("🟢 Connected", style="green")
    labels = {
        "unauthorized": "Authorization required",
        "offline": "Not responding",
        "unresponsive": "No command response",
        "no permissions": "USB permission required",
        "device": "Serial unavailable",
    }
    if connections:
        return Text(
            "🟡 " + labels.get(connections[0].state, connections[0].state.capitalize()), style="yellow"
        )
    return Text("⚪ Not connected", style="dim")


def _connection_addresses(transports: Iterable[Transport]) -> str:
    lines = []
    for transport in transports:
        kind = "Wireless" if transport.wireless else "USB"
        line = f"{kind}: {transport.address}"
        if not transport.ready:
            line += f" ({transport.state})"
        lines.append(line)
    return "\n".join(lines)


def _service_connected(service: ServiceGroup, transports: Iterable[Transport]) -> bool:
    """Match current responding wireless transports, never serial guesses or saved addresses."""
    if service.kind != ServiceKind.CONNECT:
        return False
    names = (
        {
            f"{service.instance.rstrip('.').casefold()}{suffix}{domain}"
            for suffix in ("._adb-tls-connect._tcp", "._adb._tcp")
            for domain in ("", ".local")
        }
        if service.instance
        else set()
    )
    for transport in transports:
        if not transport.ready or not transport.wireless:
            continue
        if endpoint_or_none(transport.address) in service.endpoints:
            return True
        if transport.address.rstrip(".").casefold() in names:
            return True
    return False


def _device_groups(snapshot: Snapshot) -> tuple[dict[str, list[Transport]], list[TransportGroup]]:
    groups = group_transports(snapshot.transports)
    groups.extend(TransportGroup((t,)) for t in snapshot.transports if t.state != "device")
    assigned: dict[str, list[Transport]] = {record.id: [] for record in snapshot.devices}
    unknown: list[TransportGroup] = []
    for group in groups:
        identity = group.transports[0].identity
        matches = [record for record in snapshot.devices if identity and record.matches(identity)]
        if len(matches) == 1 and not group.conflict:
            assigned[matches[0].id].extend(group.transports)
        else:
            unknown.append(group)
    return assigned, unknown


def show_snapshot(console: Console, snapshot: Snapshot, default_device: str | None = None) -> None:
    """Always render two tables using only the existing snapshot; no device I/O."""
    table = _table("Devices", "Device / serial", "Saved", "Status", "Connections", "Auto-connect")
    table.title = Text("📱 Devices", style="bold cyan")
    table.border_style = "cyan"
    table.header_style = "bold cyan"
    table.expand = True
    assigned, unknown = _device_groups(snapshot)
    for record in snapshot.devices:
        connections = assigned[record.id]
        label = record.name + (" (default)" if record.id == default_device else "")
        label += f"\n{record.model}\n{record.serial}" if record.model else f"\n{record.serial}"
        addresses = _connection_addresses(connections)
        if not addresses:
            addresses = "\n".join(f"Last wireless: {endpoint}" for endpoint in record.endpoints)
            addresses += ("\n" if addresses else "") + f"Last seen: {_last_seen(record.last_seen)}"
        table.add_row(
            Text(label),
            Text("Yes"),
            _connection_status(connections),
            Text(addresses),
            _enabled(record.auto_connect),
        )
    for group in unknown:
        identity = group.transports[0].identity
        label = f"{identity.model or 'Android'}\n{identity.serial}" if identity else "Unidentified device"
        status = (
            Text("🟡 Identity conflict", style="yellow")
            if group.conflict
            else _connection_status(group.transports)
        )
        table.add_row(
            Text(label),
            Text("No" if identity and not group.conflict else "Unverified"),
            status,
            Text(_connection_addresses(group.transports)),
            Text("—", style="dim"),
        )
    if not snapshot.devices and not unknown:
        table.add_row(Text("No devices", style="dim"), "—", "—", "—", "—")
    table.caption = "Use 'Add a device' to save a connected device or set up wireless pairing."
    if not snapshot.devices and not unknown:
        table.caption = "No saved devices or detected ADB connections.\n" + table.caption
    table.caption_justify = "left"
    console.print(table)

    services = _table("Wireless services", "Purpose", "Service name", "Status", "Addresses")
    services.title = Text("📡 Wireless services", style="bold magenta")
    services.box = box.SIMPLE_HEAVY
    services.border_style = "dim magenta"
    services.header_style = "bold magenta"
    services.expand = True
    services.caption = (
        "Each row is one service; it may advertise multiple addresses.\n"
        "Discovered does not confirm a connection. Connected means a responding ADB connection matches this service."
    )
    services.caption_justify = "left"
    for service in snapshot.service_groups:
        status = (
            Text("🟢 Connected", style="green")
            if _service_connected(service, snapshot.transports)
            else Text("🔎 Discovered", style="cyan")
        )
        services.add_row(
            Text(service.kind.value.capitalize()),
            Text(service.instance or "Unnamed service"),
            status,
            Text("\n".join(service.endpoints)),
        )
    if not snapshot.services:
        services.add_row(Text("No services", style="dim"), "—", "—", "—")
        services.caption = "No wireless debugging services discovered."
    console.print(services)
    for warning in snapshot.warnings:
        console.print(Text(f"Note: {warning}", style="yellow"))


def _server_details(console: Console, output: str) -> None:
    rows: list[tuple[str, str | Text]] = []
    labels = {
        "usb_backend": "USB backend",
        "mdns_backend": "Discovery backend",
        "executable_absolute_path": "Executable",
        "log_absolute_path": "Log file",
        "os": "Operating system",
        "mdns_enabled": "Wireless discovery",
    }
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator or not key.replace("_", "").isalnum():
            _fields(console, "ADB server", [("Response", output or "No response")])
            return
        value = value.strip()
        if value.startswith('"'):
            try:
                decoded = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(decoded, str):
                    value = decoded
        label = labels.get(key, key.replace("_", " ").capitalize())
        display = _enabled(value == "true") if value in {"true", "false"} else value or "None"
        rows.append((label, display))
    _fields(console, "ADB server", rows or [("Response", "No response")])


def show_diagnostics(console: Console, report: Mapping[str, Any]) -> None:
    """Render the manager's diagnostic result without additional device queries."""
    backend = report["backend"]
    rows: list[tuple[str, str | Text]] = [("Profile storage", str(report["store"]))]
    labels = {
        "executable": "ADB executable",
        "version": "ADB version",
        "server_port": "Local server port",
        "mdns": "Wireless discovery",
        "error": "Error",
    }
    for key, value in backend.items():
        if key != "server":
            rows.append((labels.get(str(key), str(key).replace("_", " ").capitalize()), str(value)))
    _fields(console, "Connection diagnostics", rows)
    if "server" in backend:
        _server_details(console, backend["server"])
    data = report["snapshot"]
    transports = [
        Transport(
            address=item["address"],
            state=item["state"],
            identity=Identity(**item["identity"]) if item["identity"] else None,
            detail=item.get("detail", ""),
        )
        for item in data["transports"]
    ]
    snapshot = Snapshot(
        devices=[DeviceRecord(**item) for item in data["devices"]],
        transports=transports,
        services=[Service(**item) for item in data["services"]],
        warnings=data.get("warnings", []),
    )
    show_snapshot(console, snapshot)
    for transport in transports:
        if transport.detail:
            console.print(Text(f"{transport.address}: {transport.detail}", style="yellow"))
    if report["hints"]:
        console.print("Suggested next steps", style="bold yellow")
        for index, hint in enumerate(report["hints"], 1):
            console.print(Text(f"  {index}. {hint}"))
