from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .adb import AdbBackend
from .discovery import MdnsDiscovery
from .errors import DroidockError, IdentityError, SelectionError
from .interfaces import Backend, CommandBackend, Discovery
from .models import (
    AutoConnectReport,
    CommandResult,
    ConnectionEvent,
    DeviceCriteria,
    DeviceRecord,
    PairResult,
    Service,
    ServiceGroup,
    ServiceKind,
    Settings,
    Snapshot,
    State,
    Transport,
    TransportGroup,
    endpoint_or_none,
    normalize_endpoint,
    utc_now,
    valid_serial,
)
from .selection import ensure_connected, group_transports, select_service
from .store import DeviceStore


class ConnectionManager:
    """Headless API. No prompts, terminal imports, global process, or connection at import time.

    Supply Backend/Discovery implementations and an event callback to embed in another application.
    Connection attempts only verify identity; application installation is the caller's responsibility.
    """

    def __init__(
        self,
        store: DeviceStore | None = None,
        *,
        backend: Backend | None = None,
        discovery: Discovery | None = None,
        on_event: Callable[[ConnectionEvent], None] | None = None,
    ) -> None:
        self.store = store or DeviceStore()
        self._owns_backend = backend is None
        self.backend: Backend = backend or AdbBackend(self.settings)
        self.discovery: Discovery = discovery or MdnsDiscovery()
        self.on_event = on_event

    @property
    def settings(self) -> Settings:
        return self.store.read().settings

    def _emit(self, kind: str, record: DeviceRecord, message: str, endpoint: str = "") -> None:
        if self.on_event:
            self.on_event(ConnectionEvent(kind, record.id, message, endpoint))

    def configure(self, **changes: object) -> Settings:
        def update(state: State) -> Settings:
            settings = replace(state.settings, **changes)
            settings.validate()
            state.settings = settings
            return settings

        try:
            result = self.store.update(update)
        except (ValueError, TypeError) as exc:
            raise DroidockError(str(exc), code="invalid_setting") from exc
        if self._owns_backend:
            self.backend = AdbBackend(result)
        return result

    def device(self, selector: str | None = None) -> DeviceRecord:
        state = self.store.read()
        selector = selector or state.default_device
        if not selector:
            raise DroidockError("Register a device and select a default device.", code="device_not_found")
        matches = [
            d
            for d in state.devices
            if selector in (d.id, d.serial) or d.name.casefold() == selector.casefold()
        ]
        if not matches:
            endpoint = endpoint_or_none(selector)
            matches = [d for d in state.devices if endpoint and endpoint in d.endpoints]
        if len(matches) > 1:
            raise SelectionError(
                "Several saved profiles match this selector. Select a profile ID or unique name.",
                code="ambiguous_device",
                candidates=tuple(d.id for d in matches),
            )
        if not matches:
            raise DroidockError(f"Cannot identify a single saved device: {selector}", code="device_not_found")
        return matches[0]

    def ensure_connected(
        self,
        selector: str | None = None,
        *,
        endpoint: str | None = None,
        criteria: DeviceCriteria | None = None,
        name: str | None = None,
        remember: bool = True,
        reconnect: bool = True,
        discover: bool = True,
        prefer_usb: bool = True,
        attempts: int | None = None,
    ) -> Transport:
        """Select, validate, connect, and optionally save a current or discovered device.

        No prompts or terminal output. Explicit requests/default profiles never fall
        back to an unrelated device. remember=False leaves all profiles unchanged.
        """
        return ensure_connected(
            self,
            selector,
            endpoint=endpoint,
            criteria=criteria,
            name=name,
            remember=remember,
            reconnect=reconnect,
            discover=discover,
            prefer_usb=prefer_usb,
            attempts=attempts,
        )

    def device_groups(
        self,
        *,
        criteria: DeviceCriteria | None = None,
        prefer_usb: bool = True,
    ) -> list[TransportGroup]:
        """List responding devices, without discovery, registration, or connection attempts."""
        groups = group_transports(self.backend.transports(), prefer_usb=prefer_usb)
        return [
            g for g in groups if criteria is None or any(criteria.matches(t.identity) for t in g.transports)
        ]

    def run(
        self,
        arguments: Sequence[str],
        *,
        device: str | Transport | None = None,
        criteria: DeviceCriteria | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a command on a selected device or a previously acquired transport."""
        if not isinstance(self.backend, CommandBackend):
            raise DroidockError(
                "This backend does not support command execution.", code="unsupported_operation"
            )
        transport = (
            device if isinstance(device, Transport) else self.ensure_connected(device, criteria=criteria)
        )
        if criteria is not None and not criteria.matches(transport.identity):
            raise DroidockError("The device does not match the command criteria.", code="criteria_mismatch")
        return self.backend.run(
            arguments,
            serial=transport.address,
            input_text=input_text,
            timeout=timeout,
            cwd=cwd,
            check=check,
        )

    def discover_services(self) -> list[Service]:
        """Merge independent service sources without changing profiles or connections."""
        with ThreadPoolExecutor(max_workers=2) as executor:
            adb = executor.submit(self.backend.services)
            discovered = executor.submit(self.discovery.discover, self.settings.discovery_seconds)
            services: list[Service] = []
            errors: list[str] = []
            try:
                services.extend(adb.result())
            except DroidockError as exc:
                errors.append(str(exc))
            try:
                extra, warnings = discovered.result()
                services.extend(extra)
                errors.extend(warnings)
            except (DroidockError, OSError) as exc:
                errors.append(str(exc))
        if not services and errors:
            raise DroidockError("Wireless discovery failed.\n" + "\n".join(errors), code="discovery_failed")
        return sorted(set(services), key=lambda s: (s.kind, s.instance, s.endpoint, s.source))

    def select_service(self, kind: ServiceKind, selector: str | None = None) -> ServiceGroup:
        """Select a discovered service, returning all addresses for one purpose/instance."""
        from .models import group_services

        return select_service(group_services(self.discover_services()), kind, selector)

    def pair_discovered(self, code: str, selector: str | None = None) -> PairResult:
        """Select a pairing service and try its addresses; never use a connection port."""
        import re

        if not re.fullmatch(r"\d{6}", code):
            raise DroidockError("Enter the six-digit pairing code shown on the device.", code="invalid_code")
        service = self.select_service(ServiceKind.PAIRING, selector)
        for index, address in enumerate(service.endpoints[:3]):
            try:
                return self.pair(address, code)
            except DroidockError as exc:
                if (
                    exc.code not in {"timeout", "adb_failed", "connect_failed"}
                    or index == min(3, len(service.endpoints)) - 1
                ):
                    raise
        raise DroidockError("No pairing addresses are available.", code="service_not_found")

    def _remember(
        self, transport: Transport, *, create: bool = False, name: str | None = None
    ) -> DeviceRecord:
        identity = transport.identity
        if not transport.ready or identity is None or not valid_serial(identity.serial):
            raise IdentityError(
                "Registration requires a responding device with a valid device serial number."
            )
        if name is not None and (not name.strip() or len(name.strip()) > 80):
            raise DroidockError("Device names must contain 1 to 80 characters.", code="invalid_name")

        def save(state: State) -> DeviceRecord:
            record = next((d for d in state.devices if d.serial == identity.serial), None)
            if record is not None and not record.matches(identity):
                raise IdentityError(
                    "Conflicting device details share the same serial number. Automatic merging was stopped."
                )
            if record is None:
                if not create:
                    raise DroidockError("This device is not registered.", code="device_not_found")
                suggested = name or f"{identity.model or 'Android'} {identity.serial[-4:]}"
                record = DeviceRecord(str(uuid.uuid4()), suggested.strip(), identity.serial)
                state.devices.append(record)
                if state.default_device is None:
                    state.default_device = record.id
            if name is not None:
                record.name = name.strip()
            if any(d.id != record.id and d.name.casefold() == record.name.casefold() for d in state.devices):
                raise DroidockError(
                    "A device already uses this name. Choose another name.", code="invalid_name"
                )
            record.manufacturer = identity.manufacturer or record.manufacturer
            record.model = identity.model or record.model
            record.last_seen = utc_now()
            if identity.wifi_guid:
                record.wifi_guids = list(dict.fromkeys([identity.wifi_guid, *record.wifi_guids]))[:8]
            endpoint = endpoint_or_none(transport.address)
            if endpoint:
                record.endpoints = list(dict.fromkeys([endpoint, *record.endpoints]))[:8]
            return record

        return self.store.update(save)

    def register(self, address: str, *, name: str | None = None) -> DeviceRecord:
        """Explicit registration of an authorized, responding transport (USB serial or IP:port)."""
        return self._remember(self.backend.inspect(address), create=True, name=name)

    def scan(self) -> Snapshot:
        """Observe current connections and advertisements; do not connect unregistered devices."""
        warnings: list[str] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            transport_future = executor.submit(self.backend.transports)
            adb_future = executor.submit(self.backend.services)
            discovery_future = executor.submit(self.discovery.discover, self.settings.discovery_seconds)
            try:
                transports = transport_future.result()
            except DroidockError as exc:
                transports = []
                warnings.append(str(exc))
            try:
                services = adb_future.result()
            except DroidockError as exc:
                services = []
                warnings.append(f"ADB wireless discovery: {exc}")
            extra, discovery_warnings = discovery_future.result()
            warnings.extend(discovery_warnings)
        # A partial ADB discovery result must not suppress the independent discovery results.
        merged = {(s.instance, s.kind, s.endpoint): s for s in [*extra, *services]}
        services = sorted(merged.values(), key=lambda s: (s.kind, s.instance, s.endpoint))
        records = self.store.read().devices
        for transport in transports:
            if transport.identity and any(d.serial == transport.identity.serial for d in records):
                try:
                    self._remember(transport)
                except IdentityError as exc:
                    warnings.append(str(exc))
        return Snapshot(self.store.read().devices, transports, services, list(dict.fromkeys(warnings)))

    @staticmethod
    def _service_matches(service: Service, record: DeviceRecord) -> bool:
        instance = service.instance.casefold()
        # Advertisements only nominate candidates. Shell identity must still match after connecting.
        return service.kind == ServiceKind.CONNECT and (
            instance in {g.casefold() for g in record.wifi_guids}
            or instance.startswith(f"adb-{record.serial.casefold()}-")
            or instance == record.serial.casefold()
        )

    def connect_endpoint(
        self, endpoint: str, *, name: str | None = None, expected: DeviceRecord | None = None
    ) -> DeviceRecord:
        endpoint = normalize_endpoint(endpoint)
        self.backend.connect(endpoint)
        transport = self.backend.inspect(endpoint)
        if expected and (not transport.identity or not expected.matches(transport.identity)):
            raise IdentityError(
                f"The device at {endpoint} does not match {expected.name}. The saved profile was not changed."
            )
        return self._remember(transport, create=expected is None, name=name)

    def connect_endpoints(
        self,
        endpoints: Iterable[str],
        *,
        name: str | None = None,
        expected: DeviceRecord | None = None,
    ) -> DeviceRecord:
        """Try up to three addresses for a selected service, verifying the successful connection."""
        candidates = list(dict.fromkeys(normalize_endpoint(endpoint) for endpoint in endpoints))[:3]
        if not candidates:
            raise DroidockError("No connection addresses were provided.", code="invalid_endpoint")
        failures = []
        for endpoint in candidates:
            try:
                return self.connect_endpoint(endpoint, name=name, expected=expected)
            except DroidockError as exc:
                # Identity, storage, configuration, and profile errors must not trigger another connection.
                if exc.code not in {"connection_error", "connect_failed", "adb_failed", "timeout"}:
                    raise
                failures.append(f"{endpoint}: {exc}")
        raise DroidockError(
            "Could not connect using the selected wireless addresses.\n" + "\n".join(failures),
            code="connect_failed",
        )

    def connect(self, selector: str | None = None, *, attempts: int | None = None) -> DeviceRecord:
        """Reconnect a saved profile using the shared selection workflow."""
        record = self.device(selector)
        self.ensure_connected(record.id, attempts=attempts)
        return self.device(record.id)

    def pair(self, endpoint: str, code: str) -> PairResult:
        """Pairing grants ADB trust, but does not claim the device is connected or registered."""
        return self.backend.pair(normalize_endpoint(endpoint), code)

    def resolve(self, selector: str | None = None, *, reconnect: bool = True) -> Transport:
        """Resolve a saved profile; use ensure_connected for unregistered devices too."""
        record = self.device(selector)
        return self.ensure_connected(record.id, reconnect=reconnect)

    def auto_connect(self) -> AutoConnectReport:
        """One bounded attempt per opted-in device; intended for startup or an application's own loop."""
        report = AutoConnectReport()
        for device in self.store.read().devices:
            if not device.auto_connect:
                continue
            try:
                self.connect(device.id, attempts=1)
                report.connected.append(device.id)
            except DroidockError as exc:
                report.errors[device.id] = str(exc)
        return report

    def watch(self, *, interval: float = 5, stop: threading.Event | None = None) -> Iterator[Snapshot]:
        """Foreground, stoppable reconnection loop; no OS startup service is installed."""
        if interval < 1:
            raise DroidockError("The refresh interval must be at least 1 second.")
        stop = stop or threading.Event()
        while not stop.is_set():
            report = self.auto_connect()
            snapshot = self.scan()
            snapshot.warnings = list(dict.fromkeys([*snapshot.warnings, *report.errors.values()]))
            yield snapshot
            if stop.wait(interval):
                return

    def update_device(
        self,
        selector: str,
        *,
        name: str | None = None,
        auto_connect: bool | None = None,
        default: bool = False,
    ) -> DeviceRecord:
        chosen = self.device(selector)

        def update(state: State) -> DeviceRecord:
            record = next(d for d in state.devices if d.id == chosen.id)
            if name is not None:
                if not name.strip() or len(name.strip()) > 80:
                    raise DroidockError("Device names must contain 1 to 80 characters.")
                if any(
                    d.id != record.id and d.name.casefold() == name.strip().casefold() for d in state.devices
                ):
                    raise DroidockError("A device already uses this name.")
                record.name = name.strip()
            if auto_connect is not None:
                record.auto_connect = auto_connect
            if default:
                state.default_device = record.id
            return record

        return self.store.update(update)

    def forget(self, selector: str) -> None:
        """Forget this tool's record only. Android's pairing keys are managed by ADB."""
        chosen = self.device(selector)

        def update(state: State) -> None:
            state.devices = [d for d in state.devices if d.id != chosen.id]
            if state.default_device == chosen.id:
                state.default_device = state.devices[0].id if state.devices else None

        self.store.update(update)

    def disconnect(self, selector: str) -> int:
        """Disconnect only verified current Wi-Fi transports and disable this record's auto-connect."""
        record = self.device(selector)
        count = 0
        for transport in self.scan().transports:
            if not transport.ready or not transport.identity or not record.matches(transport.identity):
                continue
            if endpoint_or_none(transport.address):
                self.backend.disconnect(transport.address)
                count += 1
            elif transport.wireless:
                self.backend.disconnect(transport.address)
                count += 1
        self.update_device(record.id, auto_connect=False)
        return count

    def diagnostics(self) -> dict[str, object]:
        from dataclasses import asdict

        snapshot = self.scan()
        hints: list[str] = []
        for transport in snapshot.transports:
            if transport.state == "unauthorized":
                hints.append("Unlock the device and authorize USB debugging for this PC.")
            elif transport.state == "offline":
                hints.append("The device is not responding. Try reconnecting the saved device.")
            elif transport.state == "no permissions":
                hints.append("Check USB access permissions and device drivers on this PC.")
        if not snapshot.transports:
            hints.append(
                "Check USB authorization, the data cable, device drivers, or wireless debugging settings."
            )
        if not snapshot.services:
            hints.append(
                "No wireless services were discovered. Check the network or enter the current connection IP:port."
            )
        try:
            backend = self.backend.diagnostics()
        except DroidockError as exc:
            backend = {"error": str(exc)}
        return {
            "store": str(self.store.path),
            "backend": backend,
            "snapshot": asdict(snapshot),
            "hints": list(dict.fromkeys(hints)),
        }
