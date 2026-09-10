from __future__ import annotations

from io import StringIO
from unittest.mock import Mock

import pytest
from rich.console import Console
from rich.table import Table

from droidock import DeviceRecord, Identity, Service, ServiceKind, Snapshot, Transport
from droidock.display import show_snapshot

IDENTITY = Identity("SERIAL-A", "Maker", "Phone", "wifi-guid")
IPV4 = "192.0.2.10:40001"
IPV6 = "[fe80::1234%13]:40001"
INSTANCE = "adb-SERIAL-A-random"


def tables(snapshot: Snapshot) -> tuple[Table, Table]:
    console = Mock(spec=Console)
    show_snapshot(console, snapshot, "saved")
    result = [call.args[0] for call in console.print.call_args_list if isinstance(call.args[0], Table)]
    assert len(result) == 2
    assert str(result[0].title) == "📱 Devices"
    assert str(result[1].title) == "📡 Wireless services"
    return result[0], result[1]


def render(table: Table) -> str:
    output = StringIO()
    Console(file=output, width=160, color_system=None).print(table)
    return output.getvalue()


def test_empty_snapshot_keeps_both_tables_and_guidance():
    devices, services = tables(Snapshot([], [], []))
    assert "No saved devices or detected ADB connections." in render(devices)
    assert "No wireless debugging services discovered." in render(services)


@pytest.mark.parametrize("saved", [False, True])
def test_usb_and_wireless_share_one_device_row(saved):
    records = [DeviceRecord("saved", "Office phone", IDENTITY.serial)] if saved else []
    devices, _ = tables(
        Snapshot(
            records,
            [
                Transport(IDENTITY.serial, "device", IDENTITY),
                Transport(IPV4, "device", IDENTITY),
            ],
            [],
        )
    )
    assert devices.row_count == 1
    output = render(devices)
    assert "🟢 Connected" in output
    assert f"USB: {IDENTITY.serial}" in output and f"Wireless: {IPV4}" in output
    assert ("Office phone (default)" if saved else "Phone") in output
    assert ("Yes" if saved else "No") in output


def test_saved_offline_and_unregistered_connections_share_devices_table():
    saved = DeviceRecord("saved", "Offline tablet", "OTHER", endpoints=[IPV4])
    devices, services = tables(
        Snapshot(
            [saved],
            [Transport(IDENTITY.serial, "device", IDENTITY)],
            [
                Service(INSTANCE, ServiceKind.CONNECT, IPV4),
            ],
        )
    )
    assert devices.row_count == 2
    assert "⚪ Not connected" in render(devices)
    assert f"Last wireless: {IPV4}" in render(devices)
    assert "🔎 Discovered" in render(services)


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ("unauthorized", "Authorization required"),
        ("offline", "Not responding"),
        ("unresponsive", "No command response"),
        ("no permissions", "USB permission required"),
        ("device", "Serial unavailable"),
    ],
)
def test_unverified_connections_never_claim_ready_or_connected(state, label):
    devices, services = tables(
        Snapshot(
            [],
            [Transport(IPV4, state)],
            [
                Service(INSTANCE, ServiceKind.CONNECT, IPV4),
            ],
        )
    )
    output = render(devices)
    assert f"🟡 {label}" in output and "Unverified" in output
    assert "🟢 Connected" not in output + render(services)


@pytest.mark.parametrize(
    "address",
    [
        IPV4,
        IPV6,
        INSTANCE + "._adb-tls-connect._tcp",
        INSTANCE.upper() + "._ADB-TLS-CONNECT._TCP.local.",
        INSTANCE + "._adb._tcp",
    ],
)
def test_service_matches_verified_endpoint_or_full_adb_service_selector(address):
    devices, services = tables(
        Snapshot(
            [],
            [Transport(address, "device", IDENTITY)],
            [
                Service(INSTANCE, ServiceKind.CONNECT, IPV4),
                Service(INSTANCE, ServiceKind.CONNECT, IPV6),
            ],
        )
    )
    assert services.row_count == 1
    assert "Wireless:" in render(devices)
    assert "🟢 Connected" in render(services)
    assert IPV4 in render(services) and IPV6 in render(services)


@pytest.mark.parametrize(
    "address",
    [
        IDENTITY.serial,
        "192.0.2.10:40002",
        "[fe80::1234%14]:40001",
        "[fe80::1234]:40001",
        "adb-SERIAL-A-other._adb-tls-connect._tcp",
        INSTANCE + "._adb-tls-pairing._tcp",
    ],
)
def test_usb_serial_hints_other_ports_and_ipv6_scopes_do_not_match_service(address):
    _, services = tables(
        Snapshot(
            [],
            [Transport(address, "device", IDENTITY)],
            [
                Service(INSTANCE, ServiceKind.CONNECT, IPV4),
                Service(INSTANCE, ServiceKind.CONNECT, IPV6),
            ],
        )
    )
    assert "🔎 Discovered" in render(services)
    assert "🟢 Connected" not in render(services)


def test_pairing_service_is_not_marked_connected_even_at_matching_address():
    _, services = tables(
        Snapshot(
            [],
            [Transport(IPV4, "device", IDENTITY)],
            [
                Service(INSTANCE, ServiceKind.PAIRING, IPV4),
            ],
        )
    )
    assert "🔎 Discovered" in render(services)


def test_conflicting_serials_are_not_combined_or_attached_to_saved_profile():
    devices, _ = tables(
        Snapshot(
            [DeviceRecord("saved", "Saved phone", IDENTITY.serial)],
            [
                Transport("USB-A", "device", IDENTITY),
                Transport("USB-B", "device", IDENTITY),
            ],
            [],
        )
    )
    assert devices.row_count == 3
    assert render(devices).count("Identity conflict") == 2
    assert "🟢 Connected" not in render(devices)
