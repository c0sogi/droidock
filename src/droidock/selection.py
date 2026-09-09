"""Reusable selection and connection workflows, independent of terminal interfaces."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .errors import DroidockError, IdentityError, SelectionError
from .models import (
    DeviceCriteria,
    DeviceRecord,
    Identity,
    ServiceGroup,
    ServiceKind,
    Transport,
    TransportGroup,
    endpoint_or_none,
    group_services,
    normalize_endpoint,
    valid_serial,
)

if TYPE_CHECKING:
    from .manager import ConnectionManager

RETRYABLE = {"connection_error", "connect_failed", "adb_failed", "timeout", "device_unavailable"}


def group_transports(transports: Iterable[Transport], *, prefer_usb: bool = True) -> list[TransportGroup]:
    """Group serial observations, never conflating missing or conflicting identities.

    Serial/model matching is a compatibility heuristic, not proof against cloned
    identifiers. Backends may supply identities derived from other device identifiers.
    Two distinct wired connections or conflicting live Wi-Fi GUIDs are kept apart.
    """
    groups: dict[tuple[str, str], list[Transport]] = {}
    for transport in {t.address: t for t in transports if t.state == "device"}.values():
        identity = transport.identity
        key = (
            ("serial", identity.serial)
            if identity and valid_serial(identity.serial)
            else ("address", transport.address)
        )
        groups.setdefault(key, []).append(transport)
    result: list[TransportGroup] = []
    for entries in groups.values():
        identities = [t.identity for t in entries if t.identity]
        conflicts = (
            any(
                len({getattr(identity, field) for identity in identities if getattr(identity, field)}) > 1
                for field in ("manufacturer", "model", "wifi_guid")
            )
            or sum(not t.wireless for t in entries) > 1
        )
        if conflicts:
            result.extend(
                TransportGroup((t,), "Conflicting connections report the same device serial.")
                for t in entries
            )
        else:
            ordered = sorted(entries, key=lambda t: (t.wireless if prefer_usb else not t.wireless, t.address))
            result.append(TransportGroup(tuple(ordered)))
    return sorted(result, key=lambda group: group.transports[0].address)


def select_service(
    groups: Iterable[ServiceGroup], kind: ServiceKind, selector: str | None = None
) -> ServiceGroup:
    """Select a service by purpose and optional instance/address without connecting."""
    kind = ServiceKind(kind)
    selector = selector.strip() if selector else ""
    endpoint = endpoint_or_none(selector)
    candidates = [
        group
        for group in groups
        if group.kind == kind
        and (
            not selector
            or group.instance.rstrip(".").casefold() == selector.rstrip(".").casefold()
            or endpoint in group.endpoints
        )
    ]
    if len(candidates) != 1:
        raise SelectionError(
            "Choose a wireless service." if candidates else "No matching wireless service was found.",
            code="ambiguous_service" if candidates else "service_not_found",
            candidates=tuple(endpoint for group in candidates for endpoint in group.endpoints),
        )
    return candidates[0]


def ensure_connected(
    manager: ConnectionManager,
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
    """Select, connect, validate, and optionally register without application-side branching.

    Explicit identity/default requests never fall back to another device. Unknown
    responding connections are usable with remember=False; they are never persisted
    as stable identities. Discovery names are hints, and all candidates are inspected.
    """
    selector = selector.strip() if selector else ""
    endpoint = normalize_endpoint(endpoint) if endpoint is not None else None
    criteria = criteria or DeviceCriteria()
    attempts = manager.settings.connect_attempts if attempts is None else attempts
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise DroidockError("Connection attempts must be between 1 and 5.", code="invalid_setting")
    if name is not None and (not remember or not name.strip() or len(name.strip()) > 80):
        raise DroidockError("A saved device name must contain 1 to 80 characters.", code="invalid_name")
    record: DeviceRecord | None = None
    if selector:
        try:
            record = manager.device(selector)
        except DroidockError as exc:
            if exc.code != "device_not_found":
                raise
    elif endpoint is None:
        state = manager.store.read()
        record = next(
            (d for d in state.devices if d.id == state.default_device and _matches(criteria, d)), None
        )
    if record is not None and not _matches(criteria, record):
        raise DroidockError(
            "The selected profile does not match the device criteria.", code="criteria_mismatch"
        )
    expected_serial = (
        record.serial if record else (selector if selector and not endpoint_or_none(selector) else "")
    )
    observed: list[Transport] = []

    def finish(transport: Transport) -> Transport:
        if transport.state != "device":
            raise DroidockError("The selected device is not responding.", code="device_unavailable")
        if record and (not transport.identity or not record.matches(transport.identity)):
            raise IdentityError("The connection does not match the selected saved device.")
        if expected_serial and (not transport.identity or transport.identity.serial != expected_serial):
            # A current ADB transport address is also a valid explicit selector.
            if record or transport.address != selector:
                raise IdentityError("The connection does not match the requested device identity.")
        if not criteria.matches(transport.identity):
            raise DroidockError(
                "The connected device does not match the device criteria.", code="criteria_mismatch"
            )
        if remember:
            # A newly connected address may conflict with a connection already observed.
            for group in group_transports([*observed, transport]):
                if group.conflict and any(t.address == transport.address for t in group.transports):
                    raise IdentityError(group.conflict)
            saved = manager._remember(transport, create=True, name=name)
            manager._emit("connected", saved, "Connection verified.", transport.address)
        elif record:
            manager._emit("connected", record, "Connection verified.", transport.address)
        return transport

    def matches_current(transport: Transport) -> bool:
        if endpoint:
            return transport.address == endpoint
        if record:
            return bool(transport.identity and record.matches(transport.identity))
        if selector:
            return selector in (transport.address, transport.identity.serial if transport.identity else "")
        return criteria.matches(transport.identity)

    def current() -> Transport | None:
        nonlocal observed
        observed = manager.backend.transports()
        groups = group_transports(observed, prefer_usb=prefer_usb)
        candidates = [g for g in groups if any(matches_current(t) for t in g.transports)]
        if len(candidates) > 1:
            raise SelectionError(
                "Multiple devices match. Select a device name, ID, or address.",
                code="ambiguous_device",
                candidates=tuple(t.address for group in candidates for t in group.transports),
            )
        if not candidates:
            return None
        group = candidates[0]
        if group.conflict and remember:
            raise IdentityError(group.conflict)
        chosen = next(
            (t for t in group.transports if t.address == (endpoint or selector)), group.transports[0]
        )
        return finish(chosen)

    connected = current()
    if connected is not None:
        return connected

    if not selector and endpoint is None and record is None:
        matching = [d for d in manager.store.read().devices if _matches(criteria, d)]
        enabled = [d for d in matching if d.auto_connect]
        if len(enabled) > 1:
            raise SelectionError(
                "Multiple saved devices match. Select a device name or ID.",
                code="ambiguous_device",
                candidates=tuple(d.id for d in enabled),
            )
        if enabled:
            record = enabled[0]
        elif matching:
            raise DroidockError(
                "Automatic connection is disabled for the matching profiles.", code="auto_connect_disabled"
            )
    if not reconnect:
        raise DroidockError("The selected device is not connected.", code="device_unavailable")
    if record and not selector and endpoint is None and not record.auto_connect:
        raise DroidockError(
            "Automatic connection is disabled for the default device.", code="auto_connect_disabled"
        )

    failures: list[str] = []
    for attempt in range(attempts):
        if attempt:
            connected = current()
            if connected is not None:
                return connected
        if record:
            manager._emit("searching", record, f"Searching for device ({attempt + 1}/{attempts})")
        direct = endpoint or (endpoint_or_none(selector) if record is None else None)
        addresses = [direct] if direct else []
        if not addresses:
            try:
                services = manager.discover_services() if discover else []
            except DroidockError as exc:
                if exc.code != "discovery_failed":
                    raise
                failures.append(str(exc))
                services = []
            connections = [s for s in services if s.kind == ServiceKind.CONNECT]
            service_groups = group_services(connections)
            hint = record or (DeviceRecord("", "", expected_serial) if expected_serial else None)
            matched = [s for s in connections if hint and manager._service_matches(s, hint)]
            if matched:
                addresses = [s.endpoint for s in matched]
            elif len(service_groups) == 1:
                addresses = list(service_groups[0].endpoints)
            elif connections and not (record and record.endpoints):
                raise SelectionError(
                    "Multiple wireless devices are visible. Select an address.",
                    code="ambiguous_service",
                    candidates=tuple(s.endpoint for s in connections),
                )
            if record:
                addresses.extend(record.endpoints)
                # Recover a selected offline USB transport before trying network candidates.
                for transport in manager.backend.transports():
                    if transport.address == record.serial and transport.state == "offline":
                        try:
                            manager.backend.reconnect(transport.address)
                            recovered = manager.backend.inspect(transport.address)
                        except DroidockError as exc:
                            if exc.code not in RETRYABLE:
                                raise
                            failures.append(str(exc))
                        else:
                            return finish(recovered)
        for address in list(dict.fromkeys(addresses))[:3]:
            if not address:
                continue
            try:
                if record:
                    manager._emit("connecting", record, "Connecting and verifying device identity.", address)
                manager.backend.connect(address)
                inspected = manager.backend.inspect(address)
            except DroidockError as exc:
                if exc.code not in RETRYABLE:
                    raise
                failures.append(str(exc))
            else:
                # Persistence and caller predicates are never retried as transport failures.
                return finish(inspected)
        if attempt + 1 < attempts:
            time.sleep(min(attempt + 1, 3))
    detail = "\n".join(dict.fromkeys(failures))
    if record:
        manager._emit("unavailable", record, "Could not connect to the saved device.")
    raise DroidockError(
        "Could not connect to the selected device. Check debugging, authorization, and discovery.\n" + detail,
        code="device_unavailable",
    )


def _matches(criteria: DeviceCriteria, record: DeviceRecord) -> bool:
    return criteria.matches(
        Identity(record.serial, record.manufacturer, record.model, next(iter(record.wifi_guids), ""))
    )
