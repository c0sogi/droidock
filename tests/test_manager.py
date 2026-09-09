from __future__ import annotations

import threading

import pytest
from conftest import FakeDiscovery, connected

from droidock import (
    ConnectionManager,
    DeviceStore,
    DroidockError,
    IdentityError,
    Service,
    ServiceKind,
    Transport,
)


def test_identity_survives_process_restart_and_ip_port_changes(rig):
    manager, backend, discovery = rig
    original = manager.register("USB-A", name="My headset")
    backend.online.clear()
    endpoint = "192.168.10.81:42387"
    backend.network[endpoint] = connected(endpoint)
    discovery.services = [Service("wifi-guid-a", ServiceKind.CONNECT, endpoint)]
    restarted = ConnectionManager(DeviceStore(manager.store.directory), backend=backend, discovery=discovery)
    result = restarted.connect()
    assert result.id == original.id
    assert result.name == "My headset"
    assert result.endpoints == [endpoint]
    assert len(restarted.store.read().devices) == 1


def test_usb_and_wireless_share_record_and_resolve_prefers_usb(rig):
    manager, backend, _ = rig
    original = manager.register("USB-A")
    endpoint = "10.0.0.1:40000"
    backend.online[endpoint] = connected(endpoint)
    second = manager.register(endpoint)
    assert second.id == original.id
    assert len(manager.scan().devices) == 1
    assert manager.resolve(reconnect=False).address == "USB-A"


def test_other_online_device_does_not_satisfy_target(rig):
    manager, backend, _ = rig
    original = manager.register("USB-A")
    backend.online = {"USB-B": connected("USB-B", "SERIAL-B")}
    with pytest.raises(DroidockError, match="Could not connect"):
        manager.connect(original.id)


def test_reassigned_ip_cannot_change_saved_identity(rig):
    manager, backend, _ = rig
    endpoint = "192.168.1.10:45678"
    backend.online[endpoint] = connected(endpoint)
    original = manager.register(endpoint)
    backend.online.clear()
    backend.network[endpoint] = connected(endpoint, "SERIAL-B")
    with pytest.raises(DroidockError):
        manager.connect(original.id)
    saved = manager.device(original.id)
    assert saved.serial == "SERIAL-A"
    assert saved.wifi_guids == ["wifi-guid-a"]
    assert len(manager.store.read().devices) == 1


def test_discovery_hint_must_be_verified_by_shell(rig):
    manager, backend, discovery = rig
    original = manager.register("USB-A")
    backend.online.clear()
    endpoint = "192.168.1.15:40001"
    discovery.services = [Service("adb-SERIAL-A-random", ServiceKind.CONNECT, endpoint)]
    backend.network[endpoint] = connected(endpoint, "SERIAL-B")
    with pytest.raises(DroidockError):
        manager.connect(original.id)
    assert manager.device(original.id).endpoints == []


def test_new_discovery_address_precedes_old_saved_address(rig):
    manager, backend, discovery = rig
    old, new = "10.0.0.1:40001", "10.0.0.9:40002"
    backend.online[old] = connected(old)
    record = manager.register(old)
    backend.online.clear()
    backend.network[new] = connected(new)
    discovery.services = [Service("adb-SERIAL-A-random", ServiceKind.CONNECT, new)]
    manager.connect(record.id)
    assert backend.connected == [new]
    assert manager.device(record.id).endpoints[:2] == [new, old]


def test_pairing_port_is_never_used_as_connection_port(rig):
    manager, backend, discovery = rig
    record = manager.register("USB-A")
    backend.online.clear()
    discovery.services = [Service("wifi-guid-a", ServiceKind.PAIRING, "10.0.0.1:40001")]
    with pytest.raises(DroidockError):
        manager.connect(record.id)
    assert backend.connected == []


def test_partial_adb_discovery_does_not_hide_other_results(rig):
    manager, backend, discovery = rig
    backend.advertisements = [Service("one", ServiceKind.CONNECT, "10.0.0.1:40001")]
    discovery.services = [Service("two", ServiceKind.PAIRING, "10.0.0.2:40002")]
    assert len(manager.scan().services) == 2


def test_same_serial_different_model_is_not_merged(rig):
    manager, backend, _ = rig
    manager.register("USB-A")
    backend.online["USB-B"] = connected("USB-B", model="Different model")
    with pytest.raises(IdentityError):
        manager.register("USB-B")
    assert manager.store.read().devices[0].model == "Phone"


def test_no_identity_cannot_be_registered_as_an_ip(rig):
    manager, backend, _ = rig
    backend.online["10.0.0.1:40001"] = Transport("10.0.0.1:40001", "device")
    with pytest.raises(IdentityError):
        manager.register("10.0.0.1:40001")


def test_autoconnect_skips_disabled_and_unregistered_devices(rig):
    manager, backend, discovery = rig
    record = manager.register("USB-A")
    manager.update_device(record.id, auto_connect=False)
    backend.online.clear()
    discovery.services = [Service("unknown-device", ServiceKind.CONNECT, "10.0.0.8:40001")]
    assert manager.auto_connect().connected == []
    assert backend.connected == []


def test_disconnect_targets_verified_wifi_only_and_disables_autoconnect(rig):
    manager, backend, _ = rig
    record = manager.register("USB-A")
    ours, other = "10.0.0.1:40001", "10.0.0.2:40002"
    backend.online[ours] = connected(ours)
    backend.online[other] = connected(other, "SERIAL-B")
    assert manager.disconnect(record.id) == 1
    assert backend.disconnected == [ours]
    assert "USB-A" in backend.online and other in backend.online
    assert not manager.device(record.id).auto_connect


def test_forget_does_not_disconnect_or_change_other_profiles(rig):
    manager, backend, _ = rig
    first = manager.register("USB-A")
    backend.online["USB-B"] = connected("USB-B", "SERIAL-B")
    second = manager.register("USB-B", name="Second")
    manager.forget(first.id)
    assert manager.device().id == second.id
    assert backend.disconnected == []


def test_core_is_reusable_with_event_callback_and_stoppable_loop(rig):
    original, backend, _ = rig
    events = []
    manager = ConnectionManager(
        original.store, backend=backend, discovery=FakeDiscovery(), on_event=events.append
    )
    record = manager.register("USB-A")
    manager.connect(record.id)
    assert events[-1].kind == "connected"
    assert events[-1].device_id == record.id
    stop = threading.Event()
    stream = manager.watch(stop=stop)
    assert next(stream).devices[0].id == record.id
    stop.set()
    with pytest.raises(StopIteration):
        next(stream)


def test_watch_includes_failed_reconnection_reason(rig):
    manager, backend, _ = rig
    manager.register("USB-A")
    backend.online.clear()
    stream = manager.watch()
    snapshot = next(stream)
    assert any("Could not connect" in warning for warning in snapshot.warnings)
    stream.close()
