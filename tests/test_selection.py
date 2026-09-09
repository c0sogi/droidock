from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from conftest import connected
from typer.testing import CliRunner

from droidock import (
    CommandResult,
    DeviceCriteria,
    DroidockError,
    IdentityError,
    SelectionError,
    Service,
    ServiceKind,
    Transport,
    group_services,
    group_transports,
    select_service,
)
from droidock.cli import app

V4 = "10.12.4.8:40001"
V6 = "[fe80::12%15]:40002"


def test_acquire_unregistered_usb_without_discovery_and_save_alias(rig):
    manager, backend, discovery = rig
    with patch.object(discovery, "discover", side_effect=AssertionError("Unexpected discovery")):
        result = manager.ensure_connected(name="Development phone")
    assert result.address == "USB-A"
    assert backend.connected == []
    assert manager.device("Development phone").serial == "SERIAL-A"


def test_cli_uses_common_acquisition_for_unregistered_devices(rig):
    manager, _, _ = rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, ["connect", "--name", "Lab tablet", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["name"] == "Lab tablet"


@pytest.mark.parametrize("model", ["Phone", "Tablet", "Headset", "Custom industrial device"])
def test_criteria_selects_any_caller_supplied_model(rig, model):
    manager, backend, _ = rig
    backend.online["USB-B"] = connected("USB-B", "SERIAL-B", model=model)
    result = manager.ensure_connected(criteria=DeviceCriteria(models=(model.lower(),), serials=("SERIAL-B",)))
    assert result.address == "USB-B"
    assert [d.serial for d in manager.store.read().devices] == ["SERIAL-B"]


def test_predicate_can_express_additional_identity_constraints(rig):
    manager, _, _ = rig
    result = manager.ensure_connected(
        criteria=DeviceCriteria(predicate=lambda i: i.wifi_guid == "wifi-guid-a")
    )
    assert result.address == "USB-A"


def test_ambiguous_devices_are_reported_without_registration_or_connect(rig):
    manager, backend, _ = rig
    backend.online["USB-B"] = connected("USB-B", "SERIAL-B")
    before = manager.store.path.read_bytes()
    with pytest.raises(SelectionError) as error:
        manager.ensure_connected()
    assert error.value.code == "ambiguous_device"
    assert set(error.value.candidates) == {"USB-A", "USB-B"}
    assert backend.connected == []
    assert manager.store.path.read_bytes() == before


def test_usb_and_wireless_are_grouped_with_configurable_preference(rig):
    manager, backend, _ = rig
    backend.online[V4] = connected(V4)
    before = manager.store.path.read_bytes()
    groups = manager.device_groups()
    assert len(groups) == 1 and len(groups[0].transports) == 2
    assert manager.ensure_connected(remember=False).address == "USB-A"
    assert manager.ensure_connected(remember=False, prefer_usb=False).address == V4
    assert manager.ensure_connected(V4, remember=False).address == V4
    assert manager.store.path.read_bytes() == before


def test_conflicting_serial_connections_remain_visible_and_cannot_be_merged(rig):
    manager, backend, _ = rig
    backend.online["USB-B"] = connected("USB-B")
    groups = manager.device_groups()
    assert len(groups) == 2 and all(group.conflict for group in groups)
    with pytest.raises(IdentityError):
        manager.ensure_connected("USB-A")
    assert manager.ensure_connected("USB-A", remember=False).address == "USB-A"


def test_conflicting_wireless_guids_are_not_merged():
    groups = group_transports([connected(V4), connected(V6, guid="other-guid")])
    assert len(groups) == 2 and all(group.conflict for group in groups)


def test_missing_identity_remains_usable_without_persistence(rig):
    manager, backend, _ = rig
    backend.online = {V4: Transport(V4, "device"), V6: Transport(V6, "device")}
    before = manager.store.path.read_bytes()
    assert len(manager.device_groups()) == 2
    assert manager.ensure_connected(V4, remember=False).address == V4
    with pytest.raises(IdentityError):
        manager.ensure_connected(V4)
    assert manager.store.path.read_bytes() == before


def test_disabled_default_does_not_reconnect_but_explicit_request_can(rig):
    manager, backend, discovery = rig
    record = manager.register("USB-A", name="Saved phone")
    manager.update_device(record.id, auto_connect=False)
    backend.online.clear()
    backend.network[V4] = connected(V4)
    discovery.services = [Service("opaque-name", ServiceKind.CONNECT, V4)]
    with pytest.raises(DroidockError) as error:
        manager.ensure_connected()
    assert error.value.code == "auto_connect_disabled"
    assert backend.connected == []
    assert manager.ensure_connected("Saved phone").address == V4


def test_unavailable_default_never_selects_another_current_device(rig):
    manager, backend, _ = rig
    manager.register("USB-A")
    backend.online = {"USB-B": connected("USB-B", "SERIAL-B")}
    with pytest.raises(DroidockError):
        manager.ensure_connected(discover=False)
    assert [d.serial for d in manager.store.read().devices] == ["SERIAL-A"]


def test_no_default_uses_single_matching_autoconnect_profile(rig):
    manager, backend, discovery = rig
    original = manager.register("USB-A")
    manager.store.update(lambda state: setattr(state, "default_device", None))
    backend.online.clear()
    backend.network[V4] = connected(V4)
    discovery.services = [Service("different-service-format", ServiceKind.CONNECT, V4)]
    assert manager.ensure_connected().address == V4
    assert manager.device(original.id).endpoints == [V4]


def test_old_endpoint_selects_saved_identity_using_new_discovery_address(rig):
    manager, backend, discovery = rig
    backend.online[V4] = connected(V4)
    original = manager.register(V4, name="Remembered")
    backend.online.clear()
    backend.network[V6] = connected(V6)
    discovery.services = [Service("wifi-guid-a", ServiceKind.CONNECT, V6)]
    assert manager.ensure_connected(V4).address == V6
    assert backend.connected == [V6]
    assert manager.device("Remembered").id == original.id


def test_ambiguous_historical_address_does_not_guess(rig):
    manager, backend, _ = rig
    backend.online[V4] = connected(V4)
    manager.register(V4)
    backend.online[V4] = connected(V4, "SERIAL-B")
    manager.register(V4)
    with pytest.raises(SelectionError) as error:
        manager.ensure_connected(V4)
    assert error.value.code == "ambiguous_device"
    assert backend.connected == []


def test_identity_and_criteria_are_checked_before_any_registration(rig):
    manager, backend, _ = rig
    backend.online.clear()
    backend.network[V4] = connected(V4, "SERIAL-B", model="Tablet")
    before = manager.store.path.read_bytes()
    with pytest.raises(IdentityError):
        manager.ensure_connected("SERIAL-A", endpoint=V4)
    with pytest.raises(DroidockError) as error:
        manager.ensure_connected(endpoint=V4, criteria=DeviceCriteria(models=("Phone",)))
    assert error.value.code == "criteria_mismatch"
    assert manager.store.path.read_bytes() == before


def test_named_endpoint_override_verifies_saved_profile_before_saving(rig):
    manager, backend, _ = rig
    original = manager.register("USB-A", name="Device")
    backend.network[V4] = connected(V4, "SERIAL-B")
    before = manager.store.path.read_bytes()
    with pytest.raises(IdentityError):
        manager.ensure_connected(original.name, endpoint=V4)
    assert manager.store.path.read_bytes() == before


def test_discovery_failure_still_uses_saved_endpoint_without_rewriting_when_requested(rig, monkeypatch):
    manager, backend, discovery = rig
    backend.online[V4] = connected(V4)
    record = manager.register(V4)
    backend.online.clear()
    backend.network[V4] = connected(V4)
    monkeypatch.setattr(discovery, "discover", lambda _: ([], ["Multicast unavailable"]))
    before = manager.store.path.read_bytes()
    assert manager.ensure_connected(record.id, remember=False).address == V4
    assert manager.store.path.read_bytes() == before


def test_no_reconnect_and_no_discovery_flags_do_not_connect(rig):
    manager, backend, discovery = rig
    backend.online.clear()
    with patch.object(discovery, "discover", side_effect=AssertionError("Unexpected discovery")):
        with pytest.raises(DroidockError):
            manager.ensure_connected(endpoint=V4, reconnect=False)
        with pytest.raises(DroidockError):
            manager.ensure_connected(discover=False)
    assert backend.connected == []


def test_opaque_wireless_service_tries_both_addresses_and_saves_once(rig):
    manager, backend, discovery = rig
    backend.online.clear()
    backend.network[V6] = connected(V6)
    discovery.services = [Service("opaque", ServiceKind.CONNECT, a) for a in (V4, V6)]
    assert manager.ensure_connected(name="Wireless tablet").address == V6
    assert backend.connected == [V4, V6]
    assert len(manager.store.read().devices) == 1


def test_multiple_unnamed_advertisements_are_not_assumed_to_be_one_device(rig):
    manager, backend, discovery = rig
    backend.online.clear()
    discovery.services = [Service("", ServiceKind.CONNECT, a) for a in (V4, V6)]
    with pytest.raises(SelectionError):
        manager.ensure_connected()
    assert backend.connected == []


def test_new_wireless_physical_serial_is_verified_even_with_arbitrary_service_name(rig):
    manager, backend, discovery = rig
    backend.online.clear()
    discovery.services = [Service("opaque", ServiceKind.CONNECT, V4)]
    backend.network[V4] = connected(V4, "SERIAL-B")
    with pytest.raises(IdentityError):
        manager.ensure_connected("SERIAL-A")
    assert manager.store.read().devices == []


def test_service_selection_preserves_purpose_and_all_addresses(rig):
    manager, _, discovery = rig
    discovery.services = [Service("opaque", ServiceKind.PAIRING, a) for a in (V4, V6)]
    discovery.services.append(Service("opaque", ServiceKind.CONNECT, "10.12.4.8:49999"))
    group = manager.select_service(ServiceKind.PAIRING, "OPAQUE.")
    assert group.endpoints == (V4, V6)
    assert select_service(group_services(discovery.services), ServiceKind.PAIRING, V6) == group


def test_pairing_falls_back_on_transport_failure_but_not_invalid_code(rig, monkeypatch):
    manager, backend, discovery = rig
    discovery.services = [Service("pair", ServiceKind.PAIRING, a) for a in (V4, V6)]
    pair = backend.pair
    calls = []

    def attempt(address, code):
        calls.append(address)
        if address == V4:
            raise DroidockError("Unavailable", code="timeout")
        return pair(address, code)

    monkeypatch.setattr(backend, "pair", attempt)
    assert manager.pair_discovered("123456").endpoint == V6
    assert calls == [V4, V6]
    with patch.object(discovery, "discover", side_effect=AssertionError("Unexpected discovery")):
        with pytest.raises(DroidockError):
            manager.pair_discovered("bad")


def test_command_dispatch_uses_selected_backend_and_transport(rig, monkeypatch):
    manager, backend, _ = rig
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return CommandResult(0, "Phone\n", "")

    monkeypatch.setattr(backend, "run", run, raising=False)
    result = manager.run(["shell", "getprop", "ro.product.model"], timeout=45)
    assert result.stdout == "Phone\n"
    assert calls[0][1]["serial"] == "USB-A"
    assert calls[0][1]["timeout"] == 45


def test_optional_command_capability_does_not_break_custom_backends(rig):
    manager, _, _ = rig
    with pytest.raises(DroidockError) as error:
        manager.run(["shell", "echo", "hello"])
    assert error.value.code == "unsupported_operation"
    assert manager.store.read().devices == []


def test_storage_failure_is_not_retried_as_another_connection(rig):
    manager, backend, discovery = rig
    backend.online.clear()
    backend.network = {V4: connected(V4), V6: connected(V6)}
    discovery.services = [Service("opaque", ServiceKind.CONNECT, a) for a in (V4, V6)]
    with patch.object(manager.store, "update", side_effect=DroidockError("Storage failed")):
        with pytest.raises(DroidockError, match="Storage failed"):
            manager.ensure_connected()
    assert backend.connected == [V4]


def test_retry_checks_new_usb_connection_before_repeating_wireless_discovery(rig, monkeypatch):
    manager, backend, _ = rig
    manager.register("USB-A")
    backend.online.clear()
    monkeypatch.setattr(
        "droidock.selection.time.sleep", lambda _: backend.online.update({"USB-A": connected()})
    )
    assert manager.ensure_connected(attempts=2, discover=False).address == "USB-A"
    assert backend.connected == []


def test_new_connection_cannot_merge_conflicting_live_identity(rig):
    manager, backend, _ = rig
    backend.network[V4] = connected(V4, guid="different-live-guid")
    before = manager.store.path.read_bytes()
    with pytest.raises(IdentityError):
        manager.ensure_connected(endpoint=V4)
    assert manager.store.path.read_bytes() == before
