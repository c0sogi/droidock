"""Importable Android connection management. Importing this package performs no device I/O."""

from .adb import AdbBackend
from .discovery import MdnsDiscovery
from .errors import CommandError, DroidockError, IdentityError, SelectionError
from .interfaces import Backend, CommandBackend, Discovery
from .manager import ConnectionManager
from .models import (
    AutoConnectReport,
    CommandResult,
    ConnectionEvent,
    DeviceCriteria,
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
    group_services,
)
from .network_discovery import CompositeDiscovery, UnicastDiscovery
from .portscan import AdbPortScanner, PortScanProgress, PortScanStatus, preferred_adb_ports
from .selection import group_transports, select_service
from .store import DeviceStore
from .tailscale import TailscaleClient, TailscalePeer

__version__ = "0.1.2"
__all__ = [
    "AdbBackend",
    "CommandBackend",
    "CommandError",
    "CommandResult",
    "CompositeDiscovery",
    "DeviceCriteria",
    "MdnsDiscovery",
    "SelectionError",
    "TransportGroup",
    "UnicastDiscovery",
    "group_transports",
    "select_service",
    "AdbPortScanner",
    "DroidockError",
    "AutoConnectReport",
    "Backend",
    "ConnectionEvent",
    "ConnectionManager",
    "DeviceRecord",
    "DeviceStore",
    "Discovery",
    "Identity",
    "IdentityError",
    "PairResult",
    "PortScanProgress",
    "PortScanStatus",
    "Service",
    "ServiceGroup",
    "ServiceKind",
    "Settings",
    "Snapshot",
    "Transport",
    "TailscaleClient",
    "TailscalePeer",
    "group_services",
    "preferred_adb_ports",
]
