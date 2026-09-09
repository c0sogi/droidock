"""Importable Android connection management. Importing this package performs no device I/O."""

from .errors import DroidockError, IdentityError
from .interfaces import Backend, Discovery
from .manager import ConnectionManager
from .models import (
    AutoConnectReport,
    ConnectionEvent,
    DeviceRecord,
    Identity,
    PairResult,
    Service,
    ServiceGroup,
    ServiceKind,
    Settings,
    Snapshot,
    Transport,
    group_services,
)
from .portscan import AdbPortScanner, PortScanProgress, PortScanStatus, preferred_adb_ports
from .store import DeviceStore
from .tailscale import TailscaleClient, TailscalePeer

__version__ = "0.1.1"
__all__ = [
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
