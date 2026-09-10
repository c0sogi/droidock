from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .errors import DroidockError


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_endpoint(value: str) -> str:
    """Validate host:port (including [IPv6]:port); never guess port 5555."""
    value = value.strip()
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", value)
        if not match:
            raise DroidockError("Use the address format [IPv6]:port.", code="invalid_endpoint")
        host, port_text = match.groups()
        try:
            if ipaddress.ip_address(host).version != 6:
                raise ValueError
        except ValueError as exc:
            raise DroidockError("Invalid IPv6 address.", code="invalid_endpoint") from exc
        rendered_host = f"[{host}]"
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
            raise DroidockError("Use the address format IP:port or hostname:port.", code="invalid_endpoint")
        rendered_host = host.lower()
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise DroidockError("The port must be between 1 and 65535.", code="invalid_endpoint")
    return f"{rendered_host}:{int(port_text)}"


def endpoint_or_none(value: str) -> str | None:
    try:
        return normalize_endpoint(value)
    except DroidockError:
        return None


def valid_serial(value: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9._:-]{3,128}", value)
        and value.casefold() not in {"unknown", "none", "null", "0123456789abcdef", "1234567890"}
        and set(value) != {"0"}
    )


@dataclass(frozen=True)
class Identity:
    serial: str
    manufacturer: str = ""
    model: str = ""
    wifi_guid: str = ""


@dataclass(frozen=True)
class Transport:
    address: str
    state: str
    identity: Identity | None = None
    detail: str = ""

    @property
    def ready(self) -> bool:
        # Identity is obtained through a successful shell response, not a devices-list entry.
        return self.state == "device" and self.identity is not None

    @property
    def wireless(self) -> bool:
        return endpoint_or_none(self.address) is not None or "._tcp" in self.address.casefold()


@dataclass(frozen=True)
class DeviceCriteria:
    """Optional caller-supplied constraints; no manufacturer or model is built in.

    String fields use case-insensitive exact matching. The predicate can inspect
    additional identity properties. Unknown identity only matches empty criteria.
    """

    models: tuple[str, ...] = ()
    manufacturers: tuple[str, ...] = ()
    serials: tuple[str, ...] = ()
    predicate: Callable[[Identity], bool] | None = field(default=None, repr=False, compare=False)

    def matches(self, identity: Identity | None) -> bool:
        if identity is None:
            return not (self.models or self.manufacturers or self.serials or self.predicate)
        for observed, allowed in (
            (identity.model, self.models),
            (identity.manufacturer, self.manufacturers),
            (identity.serial, self.serials),
        ):
            if allowed and observed.casefold() not in {value.casefold() for value in allowed}:
                return False
        return self.predicate is None or self.predicate(identity)


@dataclass(frozen=True)
class TransportGroup:
    """Related responding connections, or an individual unidentifiable connection.

    Conflicting observations remain separate and carry a reason for the caller's UI.
    """

    transports: tuple[Transport, ...]
    conflict: str = ""


@dataclass(frozen=True)
class CommandResult:
    """Captured ADB output. Pairing input is redacted from both output streams."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return "\n".join(value.strip() for value in (self.stdout, self.stderr) if value.strip())


class ServiceKind(StrEnum):
    """The purpose of an advertised ADB service, with stable string values for JSON."""

    CONNECT = "connect"
    PAIRING = "pairing"


ADB_SERVICE_KINDS: dict[str, ServiceKind] = {
    "_adb._tcp": ServiceKind.CONNECT,
    "_adb-tls-connect._tcp": ServiceKind.CONNECT,
    "_adb-tls-pairing._tcp": ServiceKind.PAIRING,
}


@dataclass(frozen=True)
class Service:
    instance: str
    kind: ServiceKind
    endpoint: str
    source: str = "mdns"

    def __post_init__(self) -> None:
        # Normalize strings from existing callers and JSON; reject unsupported purposes.
        object.__setattr__(self, "kind", ServiceKind(self.kind))


@dataclass(frozen=True)
class ServiceGroup:
    """Addresses for one advertised service; device identity still requires a connection."""

    instance: str
    kind: ServiceKind
    endpoints: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ServiceKind(self.kind))


def group_services(services: Iterable[Service]) -> list[ServiceGroup]:
    """Group discovery addresses by service purpose and instance, preferring IPv4 over IPv6."""
    groups: dict[tuple[ServiceKind, str, str], list[Service]] = {}
    for service in services:
        instance = service.instance.rstrip(".")
        # Do not combine unnamed advertisements just because their service purpose matches.
        key = (service.kind, instance.casefold(), "" if instance else service.endpoint)
        groups.setdefault(key, []).append(service)
    result = []
    for entries in groups.values():
        first = entries[0]
        endpoints = {normalize_endpoint(entry.endpoint) for entry in entries}
        result.append(
            ServiceGroup(
                first.instance.rstrip("."),
                first.kind,
                tuple(sorted(endpoints, key=lambda endpoint: (endpoint.startswith("["), endpoint))),
            )
        )
    return sorted(result, key=lambda group: (group.kind, group.instance.casefold(), group.endpoints))


@dataclass
class DeviceRecord:
    id: str
    name: str
    serial: str
    manufacturer: str = ""
    model: str = ""
    wifi_guids: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    last_seen: str = ""
    auto_connect: bool = True

    def matches(self, identity: Identity) -> bool:
        return self.serial == identity.serial and all(
            not saved or not observed or saved == observed
            for saved, observed in (
                (self.manufacturer, identity.manufacturer),
                (self.model, identity.model),
            )
        )


@dataclass
class Settings:
    adb_path: str = ""
    server_port: int = 5037
    command_timeout: float = 5.0
    discovery_seconds: float = 2.0
    connect_attempts: int = 3
    auto_connect_on_start: bool = True

    def validate(self) -> None:
        numeric = {
            "server_port": (self.server_port, 1, 65535),
            "command_timeout": (self.command_timeout, 1, 30),
            "discovery_seconds": (self.discovery_seconds, 0, 15),
            "connect_attempts": (self.connect_attempts, 1, 5),
        }
        for key, (value, low, high) in numeric.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
        if not isinstance(self.server_port, int) or not isinstance(self.connect_attempts, int):
            raise ValueError("server_port and connect_attempts must be integers.")
        if not isinstance(self.adb_path, str) or not isinstance(self.auto_connect_on_start, bool):
            raise ValueError("Invalid setting value.")


@dataclass
class State:
    schema_version: int = 1
    settings: Settings = field(default_factory=Settings)
    devices: list[DeviceRecord] = field(default_factory=list)
    default_device: str | None = None


@dataclass
class Snapshot:
    devices: list[DeviceRecord]
    transports: list[Transport]
    services: list[Service]
    warnings: list[str] = field(default_factory=list)

    @property
    def service_groups(self) -> list[ServiceGroup]:
        """Group IPv4/IPv6 addresses without changing the individual service records."""
        return group_services(self.services)


@dataclass(frozen=True)
class ConnectionEvent:
    kind: str
    device_id: str
    message: str
    endpoint: str = ""


@dataclass
class AutoConnectReport:
    connected: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PairResult:
    endpoint: str
    guid: str = ""
