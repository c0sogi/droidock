from __future__ import annotations

import asyncio
import getpass
import sys
from contextlib import nullcontext
from dataclasses import replace

import questionary
from rich.console import Console

from . import __version__
from .dashboard import Dashboard, DeviceEntry
from .display import (
    AdbScanDisplay,
    show_auto_connect,
    show_device,
    show_diagnostics,
    show_disconnected,
    show_snapshot,
    show_tailscale_peers,
)
from .errors import DroidockError
from .manager import ConnectionManager
from .models import DeviceRecord, ServiceGroup, ServiceKind, Snapshot
from .portscan import AdbPortScanner, PortScanStatus, endpoint_host, preferred_adb_ports
from .tailscale import TailscaleClient, TailscalePeer


class InteractiveCli:
    def __init__(
        self,
        manager: ConnectionManager,
        *,
        plain: bool = False,
        console: Console | None = None,
        tailscale: TailscaleClient | None = None,
        port_scanner: AdbPortScanner | None = None,
    ) -> None:
        self.manager = manager
        self.plain = plain
        self.console = console or Console()
        self._snapshot = Snapshot([], [], [])
        self._has_snapshot = False
        self.tailscale_client = tailscale or TailscaleClient()
        self.port_scanner = port_scanner or AdbPortScanner()
        self._tailscale_peers: list[TailscalePeer] | None = None
        self._fullscreen = False
        self._dashboard: Dashboard | None = None

    def _header(self) -> None:
        self.console.print(f"Droidock {__version__}", style="bold cyan")
        self.console.print("Remember device names and connection settings on this PC. Press Ctrl+C to exit.")
        self.console.print("Use 'Scan again' to refresh this view. Opening or leaving menus does not scan.")

    def _page(self) -> None:
        if self._fullscreen:
            self.console.clear()
            self._header()

    def _pause(self) -> None:
        if self._fullscreen:
            self.text("Press Enter to continue")

    def choose(self, title: str, choices: list[tuple[str, str]], *, back: str = "Back") -> str | None:
        if self.plain:
            self.console.print(title, style="bold", markup=False)
            for index, (label, _) in enumerate(choices, 1):
                self.console.print(f"  {index}. {label}", markup=False)
            self.console.print(f"  0. {back}")
            while True:
                try:
                    answer = input("Number > ").strip()
                except EOFError:
                    return None
                if answer in {"0", "q", "quit"}:
                    return None
                if answer.isdigit() and 1 <= int(answer) <= len(choices):
                    return choices[int(answer) - 1][1]
                self.console.print("Enter one of the listed numbers.")
        # Choice uses its title when value=None, so use a distinct value for going back.
        back_value = object()
        answer = questionary.select(
            title,
            choices=[questionary.Choice(label, value=value) for label, value in choices]
            + [questionary.Choice(back, value=back_value)],
            qmark="?",
            instruction="(Use Up/Down arrows and Enter to select)",
        ).ask()
        return None if answer is back_value else answer

    def text(self, title: str, default: str = "", *, secret: bool = False) -> str | None:
        if self.plain:
            try:
                if secret:
                    return getpass.getpass(title + " > ")
                answer = input(title + (f" [{default}]" if default else "") + " > ")
                return answer.strip() or default
            except EOFError:
                return None
        prompt = questionary.password(title) if secret else questionary.text(title, default=default)
        return prompt.ask()

    def confirm(self, title: str) -> bool:
        return self.choose(title, [("Yes", "yes")], back="No") == "yes"

    def _scan(self) -> Snapshot:
        with self.console.status("Discovering devices and current wireless addresses..."):
            self._snapshot = self.manager.scan()
        self._has_snapshot = True
        return self._snapshot

    def _refresh_connections(self) -> None:
        """Refresh command responses after an action, without another network discovery."""
        self._snapshot = replace(self._snapshot, transports=self.manager.backend.transports())

    def _connected(self, record: DeviceRecord) -> None:
        self.console.print(f"Connected: {record.name} / serial {record.serial}", style="green", markup=False)
        self.console.print(
            "Saved on this PC. Use 'Manage saved devices' to change automatic connection preferences."
        )

    def _name(self, record: DeviceRecord) -> None:
        name = self.text("Device name", record.name)
        if name is not None:
            record = self.manager.update_device(record.id, name=name)
        self._connected(record)
        self._refresh_connections()
        self._pause()

    def _endpoints(self, snapshot: Snapshot, kind: ServiceKind) -> tuple[str, ...] | None:
        while True:
            candidates = [service for service in snapshot.service_groups if service.kind == kind]
            choices = []
            for index, service in enumerate(candidates):
                preferred, *alternates = service.endpoints
                suffix = f" ({len(alternates) + 1} addresses)" if alternates else ""
                choices.append((f"{service.instance}  {preferred}{suffix}", str(index)))
            choice = self.choose(
                "Select a pairing service" if kind == ServiceKind.PAIRING else "Select a wireless connection",
                [*choices, ("Enter an address manually", "manual"), ("Scan again", "scan")],
            )
            if choice == "scan":
                snapshot = self._scan()
                continue
            if choice == "manual":
                endpoint = self.text("IP:port shown on the device")
                return (endpoint,) if endpoint else None
            return candidates[int(choice)].endpoints if choice is not None else None

    def pair(self, service: ServiceGroup | None = None) -> None:
        if service is None and not self._has_snapshot:
            self._scan()  # Standalone `droidock pair` has no initial overview snapshot.
        self.console.print(
            "On the device, open Developer options > Wireless debugging > 'Pair device with pairing code'."
        )
        endpoints = service.endpoints if service else self._endpoints(self._snapshot, ServiceKind.PAIRING)
        if not endpoints:
            return
        code = self.text("Six-digit code shown on the device", secret=True)
        if not code:
            return
        with self.console.status("Pairing..."):
            paired = self.manager.pair(endpoints[0], code)
        self.console.print("Pairing completed. Looking up the current connection address.", style="green")
        snapshot = self._scan()
        matches = [
            service
            for service in snapshot.service_groups
            if service.kind == ServiceKind.CONNECT
            and paired.guid
            and service.instance.casefold() == paired.guid.rstrip(".").casefold()
        ]
        if len(matches) == 1:
            connection_endpoints = matches[0].endpoints
        else:
            self.console.print(
                "Use 'IP address & port' on the Wireless debugging screen. The connection port differs from the pairing port."
            )
            connection_endpoints = self._endpoints(snapshot, ServiceKind.CONNECT)
        if connection_endpoints:
            with self.console.status("Connecting and verifying the device serial number..."):
                record = self.manager.connect_endpoints(connection_endpoints)
            self._name(record)
        else:
            self._pause()

    def register(self) -> None:
        action = self.choose(
            "Add a device",
            [
                ("Register a USB or already connected device", "connected"),
                ("Pair using wireless debugging", "pair"),
                ("Connect to an already paired device by address", "endpoint"),
            ],
        )
        if action == "pair":
            self.pair()
        elif action == "endpoint":
            endpoints = self._endpoints(self._snapshot, ServiceKind.CONNECT)
            if endpoints:
                with self.console.status("Connecting and verifying the device serial number..."):
                    record = self.manager.connect_endpoints(endpoints)
                self._name(record)
        elif action == "connected":
            snapshot = self._snapshot
            choices: dict[str, tuple[str, str]] = {}
            for transport in snapshot.transports:
                if transport.ready and transport.identity:
                    identity = transport.identity
                    choices.setdefault(
                        identity.serial, (f"{identity.model} / {identity.serial}", transport.address)
                    )
            if not choices:
                show_snapshot(self.console, snapshot)
                self.console.print(
                    "No responding devices. Authorize USB debugging or complete wireless pairing first."
                )
                self._pause()
                return
            address = self.choose("Select a device to register", list(choices.values()))
            if address:
                self._name(self.manager.register(address))

    def manage(self, selector: str | None = None) -> None:
        state = self.manager.store.read()
        selected = selector or self.choose("Select a saved device", [(d.name, d.id) for d in state.devices])
        if not selected:
            return
        record = self.manager.device(selected)
        self._page()
        action = self.choose(
            record.name,
            [
                ("Connect / reconnect", "connect"),
                ("Rename", "rename"),
                ("Use as default device", "default"),
                (
                    "Disable automatic connection" if record.auto_connect else "Enable automatic connection",
                    "auto",
                ),
                ("Show connection details", "details"),
                ("Disconnect wireless connections and disable automatic connection", "disconnect"),
                ("Delete this profile from the PC", "forget"),
            ],
        )
        if action == "connect":
            with self.console.status("Finding and reconnecting the saved device..."):
                connected = self.manager.connect(record.id)
            self.console.print(f"Connected: {connected.name}", style="green", markup=False)
            self._refresh_connections()
            self._pause()
        elif action == "rename":
            name = self.text("New name", record.name)
            if name:
                self.manager.update_device(record.id, name=name)
        elif action == "default":
            self.manager.update_device(record.id, default=True)
        elif action == "auto":
            self.manager.update_device(record.id, auto_connect=not record.auto_connect)
        elif action == "details":
            show_device(self.console, record, default_device=state.default_device)
            self._pause()
        elif action == "disconnect" and self.confirm(
            "Disconnect wireless debugging and disable automatic connection?"
        ):
            count = self.manager.disconnect(record.id)
            show_disconnected(self.console, count)
            self._refresh_connections()
            self._pause()
        elif action == "forget" and self.confirm(
            "Delete this profile? Android pairing authorization will be retained."
        ):
            self.manager.forget(record.id)

    def settings(self) -> None:
        settings = self.manager.settings
        self.console.print(f"Profile storage: {self.manager.store.path}", markup=False)
        selected = self.choose(
            "Connection settings",
            [
                (
                    f"Connect automatically on startup: {'Enabled' if settings.auto_connect_on_start else 'Disabled'}",
                    "startup",
                ),
                (f"Wireless discovery duration: {settings.discovery_seconds} s", "discovery_seconds"),
                (f"Command timeout: {settings.command_timeout} s", "command_timeout"),
                (f"Connection attempts: {settings.connect_attempts}", "connect_attempts"),
                (f"ADB executable: {settings.adb_path or 'Bundled ADB'}", "adb_path"),
                (f"Local ADB server port: {settings.server_port}", "server_port"),
            ],
        )
        if selected == "startup":
            self.manager.configure(auto_connect_on_start=not settings.auto_connect_on_start)
        elif selected:
            if selected == "adb_path":
                self.console.print("Enter 'auto' to use bundled ADB.")
            answer = self.text("New value", str(getattr(settings, selected)))
            if answer is None:
                return
            try:
                value: object
                if selected in {"connect_attempts", "server_port"}:
                    value = int(answer)
                elif selected in {"discovery_seconds", "command_timeout"}:
                    value = float(answer)
                else:
                    value = "" if answer == "auto" else answer
            except ValueError as exc:
                raise DroidockError("Enter a valid number.") from exc
            self.manager.configure(**{selected: value})
            if selected in {"adb_path", "server_port"}:
                self._scan()

    def _tailscale_refresh(self) -> None:
        with self.console.status("Reading the Tailscale device list..."):
            self._tailscale_peers = self.tailscale_client.peers()

    def _tailscale_find(self, peer: TailscalePeer, host: str) -> str | None:
        endpoints = [t.address for t in self._snapshot.transports]
        endpoints.extend(e for d in self.manager.store.read().devices for e in d.endpoints)
        preferred = preferred_adb_ports(host, endpoints)
        self.console.print(f"Searching {host}. Current and saved ports are checked first.", markup=False)
        self.console.print(
            "ETA estimates the time to check the remaining ports; finding ADB ends the search early. "
            f"Time limit: {self.port_scanner.total_timeout:g} seconds. Press Ctrl+C to cancel this search."
        )
        display = AdbScanDisplay(self.console)
        try:
            with display.progress:
                result = asyncio.run(
                    self.port_scanner.scan(
                        host,
                        preferred_ports=preferred,
                        excluded_ports=peer.peer_api_ports,
                        on_progress=display.update,
                    )
                )
        except KeyboardInterrupt:
            self.console.print("Search cancelled. Returning to the device menu.", style="yellow")
            self._pause()
            return None
        if result.status == PortScanStatus.CANCELLED:
            self.console.print("Search cancelled. Returning to the device menu.", style="yellow")
        elif result.endpoint:
            self.console.print(f"ADB response verified at {result.endpoint}.", style="green", markup=False)
            return result.endpoint
        else:
            self.console.print(
                f"No ADB response found after checking {result.completed:,} of {result.total:,} ports. "
                "Check Wireless debugging, the device's Tailscale connection, and network access. "
                "You can retry or select another address.",
                style="yellow",
            )
        self._pause()
        return None

    def _tailscale_device(self, peer: TailscalePeer) -> None:
        host = peer.addresses[0]
        while True:
            self._page()
            self.console.print(f"Selected address: {host}", markup=False)
            action = self.choose(
                peer.name,
                [
                    ("Find ADB port and connect", "find"),
                    ("Enter a known connection port", "port"),
                    ("Select another device address", "address"),
                ],
            )
            if action is None:
                return
            try:
                if action == "address":
                    host = self.choose("Select an address", [(a, a) for a in peer.addresses]) or host
                    continue
                endpoint = None
                if action == "find":
                    endpoint = self._tailscale_find(peer, host)
                elif action == "port":
                    port = self.text("Connection port from the Wireless debugging screen")
                    if port:
                        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                if not endpoint:
                    continue
                # The address is only a hint. A saved serial must still match after connecting.
                matches = [
                    d
                    for d in self.manager.store.read().devices
                    if any(endpoint_host(e) in peer.addresses for e in d.endpoints)
                ]
                expected = matches[0] if len(matches) == 1 else None
                with self.console.status("Connecting and verifying the device serial number..."):
                    record = self.manager.connect_endpoint(endpoint, expected=expected)
                self._name(record)
                return
            except DroidockError as exc:
                self.console.print(str(exc), style="red", markup=False)
                if exc.code in {"unauthorized", "connect_failed", "adb_failed"}:
                    self.console.print(
                        "ADB discovery does not grant access. If this PC is not paired, use 'Add a device' > "
                        "'Pair using wireless debugging', then retry the connection."
                    )
                self._pause()

    def tailscale(self) -> None:
        if self._tailscale_peers is None:
            self._tailscale_refresh()
        while True:
            self._page()
            peers = self._tailscale_peers or []
            show_tailscale_peers(self.console, peers)
            self.console.print(
                "This list is cached. 'Refresh device list' updates it. No ports are searched until you request it."
            )
            selected = self.choose(
                "Select a Tailscale device",
                [
                    (f"{p.name} ({p.os}, {'online' if p.online else 'offline'})", str(i))
                    for i, p in enumerate(peers)
                ]
                + [("Refresh device list", "refresh")],
            )
            if selected is None:
                return
            if selected == "refresh":
                self._tailscale_refresh()
            else:
                self._tailscale_device(peers[int(selected)])

    def restart_server(self) -> None:
        port = self.manager.settings.server_port
        self.console.print(
            f"Restarting the local ADB server on port {port} interrupts all apps using it. "
            "Saved profiles and pairing credentials will be kept.",
            style="yellow",
        )
        if not self.confirm("Restart ADB server and reconnect saved devices?"):
            return
        self._snapshot = Snapshot([], [], [])  # Old connections are invalid after restarting.
        with self.console.status("Restarting ADB server and reconnecting saved devices..."):
            report = self.manager.restart_server()
        self.console.print("ADB server restarted.", style="green")
        show_auto_connect(self.console, report, self.manager.store.read().devices)
        self._scan()
        self._pause()

    def _device_action(self, entry: DeviceEntry) -> None:
        if entry.record:
            self.manage(entry.record.id)
            return
        if entry.conflict:
            raise DroidockError(entry.conflict, code="identity_mismatch")
        transport = entry.transports[0]
        if not transport.ready:
            self.console.print(
                "This connection has not supplied a verified device identity. "
                "Allow debugging on the device, then check the connection again."
            )
            if self.choose("Device connection", [("Check connection again", "check")]) == "check":
                self._refresh_connections()
            return
        if self.choose(entry.label, [("Save this device and set its name", "save")]) == "save":
            current = self.manager.ensure_connected(
                transport.identity.serial if transport.identity else transport.address, remember=False
            )
            self._name(self.manager.register(current.address))

    def _service_action(self, service: ServiceGroup) -> None:
        self.console.print(service.instance, style="bold magenta", markup=False)
        self.console.print("\n".join(service.endpoints), markup=False)
        if service.kind == ServiceKind.PAIRING:
            if self.choose("Pairing service", [("Pair using the code on the device", "pair")]) == "pair":
                self.pair(service)
            return
        if self.choose("Wireless connection", [("Connect and save this device", "connect")]) != "connect":
            return
        try:
            with self.console.status("Connecting to the selected service..."):
                record = self.manager.connect_endpoints(service.endpoints)
        except DroidockError as exc:
            if exc.code not in {"connect_failed", "timeout", "adb_failed", "connection_error"}:
                raise
            # A failed address may have changed. Refresh only then, and retain the exact selected service.
            snapshot = self._scan()
            matches = [
                item
                for item in snapshot.service_groups
                if item.kind == service.kind
                and item.instance.casefold() == service.instance.casefold()
                and service.instance
            ]
            if len(matches) != 1 or matches[0].endpoints == service.endpoints:
                raise
            record = self.manager.connect_endpoints(matches[0].endpoints)
        self._name(record)

    def _main_menu(self, *, back: str = "Exit") -> str | None:
        return self.choose(
            "What would you like to do?",
            [
                ("Scan again", "scan"),
                ("Manage saved devices", "manage"),
                ("Add a device", "register"),
                ("Connect all devices with automatic connection enabled", "auto"),
                ("Connection diagnostics", "diagnose"),
                ("Connection settings", "settings"),
                ("Tailscale devices", "tailscale"),
                ("Restart ADB server and reconnect", "restart"),
            ],
            back=back,
        )

    def run(self) -> None:
        if not self.plain and not sys.stdin.isatty():
            raise DroidockError(
                "Run the interactive menu in a terminal. Use devices --json or --plain for automation.",
                code="terminal_required",
            )
        self._fullscreen = (
            not self.plain
            and self.console.is_terminal
            and not self.console.legacy_windows
            and not self.console.is_dumb_terminal
        )
        try:
            with self.console.screen(hide_cursor=False) if self._fullscreen else nullcontext():
                self._run()
        finally:
            self._fullscreen = False

    def _run(self) -> None:
        if self._fullscreen:
            self._page()
        else:
            self._header()
        if self.manager.settings.auto_connect_on_start and self.manager.store.read().devices:
            with self.console.status("Connecting to saved devices..."):
                report = self.manager.auto_connect()
            for identifier, error in report.errors.items():
                self.console.print(
                    f"{self.manager.device(identifier).name}: {error}", style="yellow", markup=False
                )
            if report.errors:
                self._pause()
        try:
            self._scan()
        except DroidockError as exc:
            # Discovery failure must still leave the menu and Exit available.
            self.console.print(str(exc), style="red", markup=False)
            self._pause()
        while True:
            try:
                if self._fullscreen:
                    # The dashboard owns its header and needs the whole terminal height.
                    self.console.clear()
                state = self.manager.store.read()
                snapshot = replace(self._snapshot, devices=state.devices)
                if self._fullscreen:
                    if self._dashboard is None:
                        self._dashboard = Dashboard(snapshot, state.default_device)
                    else:
                        self._dashboard.update(snapshot, state.default_device)
                    selection = self._dashboard.run()
                    if selection is None:
                        return
                    self._page()
                    if selection.device is not None:
                        self._device_action(selection.device)
                        continue
                    if selection.service is not None:
                        self._service_action(selection.service)
                        continue
                    action = selection.name
                    if action == "menu":
                        action = self._main_menu(back="Back")
                        if action is None:
                            continue
                else:
                    show_snapshot(self.console, snapshot, state.default_device)
                    action = self._main_menu()
                    if action is None:
                        return
                if action == "scan":
                    self._scan()
                elif action == "manage":
                    self.manage()
                elif action == "register":
                    self.register()
                elif action == "auto":
                    with self.console.status("Connecting to saved devices..."):
                        report = self.manager.auto_connect()
                    show_auto_connect(self.console, report, self.manager.store.read().devices)
                    self._scan()
                    self._pause()
                elif action == "diagnose":
                    show_diagnostics(self.console, self.manager.diagnostics())
                    self._pause()
                elif action == "restart":
                    self.restart_server()
                elif action == "settings":
                    self.settings()
                elif action == "tailscale":
                    self.tailscale()
            except DroidockError as exc:
                self.console.print(str(exc), style="red", markup=False)
                self._pause()
