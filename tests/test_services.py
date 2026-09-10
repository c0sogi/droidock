from __future__ import annotations

import json
from dataclasses import asdict
from io import StringIO
from unittest.mock import patch

import pytest
from conftest import connected
from rich.console import Console
from typer.testing import CliRunner

from droidock import (
    DroidockError,
    IdentityError,
    Service,
    ServiceGroup,
    ServiceKind,
    Snapshot,
    group_services,
)
from droidock.cli import app
from droidock.display import show_snapshot
from droidock.interactive import InteractiveCli

INSTANCE = "adb-DEMO1234567-AbCdEf"
IPV4 = "192.168.0.6:39149"
IPV6 = "[fe80::c811:e8ff:fe23:565f%15]:39149"


@pytest.fixture
def services():
    # Reproduce the user's discovery output, with IPv6 first to test address preference.
    return [
        Service(INSTANCE, ServiceKind.CONNECT, IPV6, "zeroconf"),
        Service(INSTANCE, ServiceKind.CONNECT, IPV4, "adb"),
    ]


def test_one_service_retains_both_addresses_and_raw_records(services):
    snapshot = Snapshot([], [], services)
    before = asdict(snapshot)
    groups = snapshot.service_groups
    assert len(groups) == 1
    assert groups[0].instance == INSTANCE
    assert groups[0].kind is ServiceKind.CONNECT
    assert groups[0].endpoints == (IPV4, IPV6)
    assert asdict(snapshot) == before
    assert "service_groups" not in before
    assert len(before["services"]) == 2


def test_repeated_discovery_sources_and_dns_name_variants_share_one_option(services):
    services.extend(
        [
            Service(INSTANCE.upper() + ".", ServiceKind.CONNECT, IPV4, "zeroconf"),
            Service(INSTANCE, ServiceKind.CONNECT, IPV6),
        ]
    )
    groups = group_services(services)
    assert len(groups) == 1
    assert groups[0].endpoints == (IPV4, IPV6)


@pytest.mark.parametrize(
    "other",
    [Service("another-device", ServiceKind.CONNECT, IPV4), Service(INSTANCE, ServiceKind.PAIRING, IPV4)],
)
def test_different_service_names_and_purposes_are_not_merged(services, other):
    assert len(group_services([*services, other])) == 2


def test_unnamed_services_are_not_assumed_to_be_the_same_device():
    assert (
        len(group_services([Service("", ServiceKind.CONNECT, IPV4), Service("", ServiceKind.CONNECT, IPV6)]))
        == 2
    )


def test_discovery_table_shows_one_service_with_both_literal_addresses(services):
    output = StringIO()
    show_snapshot(Console(file=output, width=120, color_system=None), Snapshot([], [], services))
    rendered = output.getvalue()
    assert rendered.count(INSTANCE) == 1
    assert sum(line.lstrip().startswith("Connect ") for line in rendered.splitlines()) == 1
    assert IPV4 in rendered and IPV6 in rendered
    assert "Each row is one service" in rendered


def test_devices_json_preserves_individual_discovery_records(rig, services):
    manager, backend, _ = rig
    backend.online.clear()
    backend.advertisements = services
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, ["devices", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {entry["endpoint"] for entry in data["services"]} == {IPV4, IPV6}
    assert len(data["services"]) == 2
    assert all(entry["kind"] == "connect" for entry in data["services"])
    assert "service_groups" not in data


def test_wireless_selector_offers_one_option_for_both_addresses(rig, services, monkeypatch):
    manager, _, _ = rig
    services.append(Service(INSTANCE, ServiceKind.PAIRING, "192.168.0.6:40001"))
    monkeypatch.setattr("builtins.input", lambda *_: "1")
    output = StringIO()
    menu = InteractiveCli(manager, plain=True, console=Console(file=output, width=120, color_system=None))
    assert menu._endpoints(Snapshot([], [], services), ServiceKind.CONNECT) == (IPV4, IPV6)
    rendered = output.getvalue()
    assert rendered.count(INSTANCE) == 1
    assert "(2 addresses)" in rendered
    assert "2. Enter an address manually" in rendered


def test_back_from_grouped_selector_does_not_connect(rig, services, monkeypatch):
    manager, backend, _ = rig
    monkeypatch.setattr("builtins.input", lambda *_: "0")
    assert (
        InteractiveCli(manager, plain=True)._endpoints(Snapshot([], [], services), ServiceKind.CONNECT)
        is None
    )
    assert backend.connected == []


@pytest.mark.parametrize("available", [IPV4, IPV6], ids=["ipv4-first", "ipv6-fallback"])
def test_interactive_registration_saves_one_named_profile(rig, services, monkeypatch, available):
    manager, backend, _ = rig
    backend.online.clear()
    backend.advertisements = services
    backend.network[available] = connected(available)
    responses = iter(["3", "3", "1", "Office Android", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    InteractiveCli(manager, plain=True).run()
    assert backend.connected == ([IPV4] if available == IPV4 else [IPV4, IPV6])
    state = manager.store.read()
    assert len(state.devices) == 1
    assert state.devices[0].name == "Office Android"
    assert state.devices[0].serial == "SERIAL-A"
    assert state.devices[0].endpoints == [available]


def test_pairing_uses_one_service_choice_then_connects_using_its_alternate_address(rig, monkeypatch):
    manager, backend, _ = rig
    pairing_ipv4, pairing_ipv6 = "192.168.0.6:40001", "[fe80::c811:e8ff:fe23:565f%15]:40001"
    backend.online.clear()
    backend.advertisements = [
        Service("pairing-service", ServiceKind.PAIRING, pairing_ipv6),
        Service("pairing-service", ServiceKind.PAIRING, pairing_ipv4),
        Service("paired-guid", ServiceKind.CONNECT, IPV6),
        Service("paired-guid", ServiceKind.CONNECT, IPV4),
    ]
    backend.network[IPV6] = connected(IPV6)
    # One pairing selection and one name prompt; no second prompt for the connection's two addresses.
    responses = iter(["1", "Office Android"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    monkeypatch.setattr("getpass.getpass", lambda *_: "765432")
    with patch.object(backend, "pair", wraps=backend.pair) as pair:
        InteractiveCli(manager, plain=True).pair()
    pair.assert_called_once_with(pairing_ipv4, "765432")
    assert backend.connected == [IPV4, IPV6]
    assert len(manager.store.read().devices) == 1
    assert manager.device().name == "Office Android"


def test_address_fallback_after_shell_timeout_preserves_saved_identity_and_alias(rig, monkeypatch):
    manager, backend, _ = rig
    original = manager.register("USB-A", name="Office Android")
    backend.online.clear()
    backend.network = {IPV4: connected(IPV4), IPV6: connected(IPV6)}
    inspect = backend.inspect

    def timeout_on_ipv4(address):
        if address == IPV4:
            raise DroidockError("No shell response", code="timeout")
        return inspect(address)

    monkeypatch.setattr(backend, "inspect", timeout_on_ipv4)
    result = manager.connect_endpoints([IPV4, IPV6], expected=original)
    assert backend.connected == [IPV4, IPV6]
    assert result.id == original.id
    assert result.name == original.name
    assert len(manager.store.read().devices) == 1


def test_identity_mismatch_stops_address_fallback_and_preserves_profile(rig):
    manager, backend, _ = rig
    original = manager.register("USB-A", name="Office Android")
    before = manager.store.path.read_bytes()
    backend.network = {IPV4: connected(IPV4, "SERIAL-B"), IPV6: connected(IPV6)}
    with pytest.raises(IdentityError):
        manager.connect_endpoints([IPV4, IPV6], expected=original)
    assert backend.connected == [IPV4]
    assert manager.store.path.read_bytes() == before


def test_invalid_profile_name_stops_address_fallback(rig):
    manager, backend, _ = rig
    before = manager.store.path.read_bytes()
    backend.network = {IPV4: connected(IPV4), IPV6: connected(IPV6)}
    with pytest.raises(DroidockError) as error:
        manager.connect_endpoints([IPV4, IPV6], name=" ")
    assert error.value.code == "invalid_name"
    assert backend.connected == [IPV4]
    assert manager.store.path.read_bytes() == before


def test_storage_failure_does_not_trigger_another_connection(rig):
    manager, backend, _ = rig
    backend.network = {IPV4: connected(IPV4), IPV6: connected(IPV6)}
    before = manager.store.path.read_bytes()
    with patch.object(
        manager.store, "update", side_effect=DroidockError("Storage unavailable", code="invalid_store")
    ):
        with pytest.raises(DroidockError, match="Storage unavailable"):
            manager.connect_endpoints([IPV4, IPV6])
    assert backend.connected == [IPV4]
    assert manager.store.path.read_bytes() == before


def test_address_fallback_is_limited_to_three_distinct_addresses(rig):
    manager, backend, _ = rig
    endpoints = [f"192.168.0.{index}:39149" for index in range(1, 5)]
    backend.network[endpoints[-1]] = connected(endpoints[-1])
    with pytest.raises(DroidockError) as error:
        manager.connect_endpoints([endpoints[0], *endpoints])
    assert error.value.code == "connect_failed"
    assert backend.connected == endpoints[:3]
    assert all(endpoint in str(error.value) for endpoint in endpoints[:3])
    assert endpoints[-1] not in str(error.value)
    assert manager.store.read().devices == []


@pytest.mark.parametrize("endpoints", [[], [IPV4, "not-an-address"]])
def test_missing_or_invalid_addresses_fail_before_connecting(rig, endpoints):
    manager, backend, _ = rig
    with pytest.raises(DroidockError) as error:
        manager.connect_endpoints(endpoints)
    assert error.value.code == "invalid_endpoint"
    assert backend.connected == []


@pytest.mark.parametrize("kind", list(ServiceKind))
def test_service_kinds_preserve_json_and_normalize_existing_string_inputs(kind):
    service = Service(INSTANCE, kind, IPV4)
    payload = json.loads(json.dumps(asdict(service)))
    assert payload == {"instance": INSTANCE, "kind": kind.value, "endpoint": IPV4, "source": "mdns"}
    restored = Service(**payload)
    assert restored.kind is kind
    assert restored == service
    assert str(restored.kind) == kind.value

    group = ServiceGroup(INSTANCE, kind, (IPV4, IPV6))
    group_payload = json.loads(json.dumps(asdict(group)))
    assert group_payload["kind"] == kind.value
    restored_group = ServiceGroup(
        group_payload["instance"], group_payload["kind"], tuple(group_payload["endpoints"])
    )
    assert restored_group.kind is kind
    assert restored_group == group


@pytest.mark.parametrize("kind", ["conect", "unknown", "Connect", None])
def test_invalid_service_kinds_are_rejected_when_loading_external_data(kind):
    payload = json.loads(json.dumps({"instance": INSTANCE, "kind": kind, "endpoint": IPV4}))
    with pytest.raises(ValueError):
        Service(**payload)
    with pytest.raises(ValueError):
        ServiceGroup(INSTANCE, payload["kind"], (IPV4, IPV6))
