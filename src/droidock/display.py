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
    Settings,
    Snapshot,
    Transport,
)
from .portscan import PortScanProgress, PortScanStatus
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


def show_snapshot(console: Console, snapshot: Snapshot, default_device: str | None = None) -> None:
    table = _table("Saved Android devices", "Device", "Status", "Current connection", "Auto-connect")
    for record in snapshot.devices:
        active = [
            t.address for t in snapshot.transports if t.ready and t.identity and record.matches(t.identity)
        ]
        table.add_row(
            Text(record.name + (" (default)" if record.id == default_device else "")),
            Text("Connected" if active else "Disconnected", style="green" if active else "yellow"),
            Text("\n".join(active) or f"Last seen: {_last_seen(record.last_seen)}"),
            _enabled(record.auto_connect),
        )
    if snapshot.devices:
        console.print(table)
    else:
        console.print(
            "No saved devices. Connect a device over USB or select 'Add a device' to set up wireless pairing."
        )
    unknown = [
        t
        for t in snapshot.transports
        if not t.identity or not any(d.matches(t.identity) for d in snapshot.devices)
    ]
    if unknown:
        found = _table("Unregistered connections", "Device / connection address", "Status")
        labels = {
            "device": "Ready to register",
            "unauthorized": "Authorization required on device",
            "offline": "Not responding",
            "unresponsive": "No command response",
            "no permissions": "USB permission required",
        }
        groups: dict[str, list[Transport]] = {}
        for transport in unknown:
            key = transport.identity.serial if transport.identity else transport.address
            groups.setdefault(key, []).append(transport)
        for group in groups.values():
            first = group[0]
            label = f"{first.identity.model} ({first.identity.serial})\n" if first.identity else ""
            status = (
                labels.get(first.state, first.state)
                if first.identity or first.state != "device"
                else "Device serial number required"
            )
            found.add_row(Text(label + "\n".join(t.address for t in group)), Text(status))
        console.print(found)
    if snapshot.services:
        services = _table("Nearby wireless debugging services", "Purpose", "Service name", "Addresses")
        services.caption = "Each row is one service; it may advertise multiple addresses."
        services.caption_justify = "left"
        for service in snapshot.service_groups:
            services.add_row(
                Text(service.kind.value.capitalize()),
                Text(service.instance),
                Text("\n".join(service.endpoints)),
            )
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
