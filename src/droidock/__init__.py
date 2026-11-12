"""Importable Android connection management. Importing this package performs no device I/O."""

from .adb import AdbBackend
from .deployment import Deployment, InstallResult
from .discovery import MdnsDiscovery
from .errors import CommandError, DroidockError, IdentityError, SelectionError
from .interfaces import Backend, CommandBackend, Discovery, ServerControlBackend
from .manager import ConnectionManager
from .mirror import (
    QUALITY_LEVELS,
    AdbLinkProbe,
    DisplayInfo,
    LinkMeasurement,
    LinkMonitor,
    MirrorEvent,
    MirrorOptions,
    MirrorQuality,
    MirrorReport,
    MirrorSession,
    MirrorStatus,
    choose_quality,
    quality_index,
    scrcpy_arguments,
)
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
from .scrcpy import SCRCPY_RELEASE, ScrcpyAsset, ScrcpyInstall, ScrcpyInstaller, ScrcpyRelease
from .selection import group_transports, select_service
from .store import DeviceStore
from .tailscale import TailscaleClient, TailscalePeer

__version__ = "0.1.7"
__all__ = [
    "QUALITY_LEVELS",
    "SCRCPY_RELEASE",
    "AdbBackend",
    "AdbLinkProbe",
    "DisplayInfo",
    "LinkMeasurement",
    "LinkMonitor",
    "MirrorEvent",
    "MirrorOptions",
    "MirrorQuality",
    "MirrorReport",
    "MirrorSession",
    "MirrorStatus",
    "ScrcpyAsset",
    "ScrcpyInstall",
    "ScrcpyInstaller",
    "ScrcpyRelease",
    "choose_quality",
    "quality_index",
    "scrcpy_arguments",
    "Deployment",
    "InstallResult",
    "CommandBackend",
    "ServerControlBackend",
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
