from __future__ import annotations

import socket
from types import SimpleNamespace

import dns.message
import dns.rrset
import pytest

from droidock import CompositeDiscovery, DroidockError, Service, ServiceKind, UnicastDiscovery
from droidock.network_discovery import parse_unicast_services


def response(*, advertised="10.4.9.12", source_only=False):
    message = dns.message.make_response(dns.message.make_query("_adb-tls-connect._tcp.local.", "PTR"))
    message.answer.append(
        dns.rrset.from_text(
            "opaque._adb-tls-connect._tcp.local.", 120, "IN", "SRV", "0 0 41321 device.local."
        )
    )
    if not source_only:
        message.additional.append(
            dns.rrset.from_text("device.local.", 120, "IN", "AAAA" if ":" in advertised else "A", advertised)
        )
    return message.to_wire()


def test_actual_interface_prefix_is_used_and_own_address_is_excluded(monkeypatch):
    adapter = SimpleNamespace(ips=[SimpleNamespace(ip="10.4.9.9", network_prefix=30)])
    monkeypatch.setattr("droidock.network_discovery.ifaddr.get_adapters", lambda: [adapter])
    hosts, warnings = UnicastDiscovery(include_local_networks=True).candidate_hosts()
    assert hosts == ["10.4.9.10"]
    assert warnings == []


def test_large_networks_are_not_silently_truncated_or_expanded():
    hosts, warnings = UnicastDiscovery(
        ["10.9.0.3"], networks=["10.0.0.0/16", "2001:db8::/64"], max_hosts=16
    ).candidate_hosts()
    assert hosts == ["10.9.0.3"]
    assert len(warnings) == 2
    assert "65534" in warnings[0]


def test_explicit_ipv6_and_combined_host_limit():
    hosts, warnings = UnicastDiscovery(
        ["fe80::1%15"], networks=["10.4.9.8/30", "10.4.9.12/30"], max_hosts=3
    ).candidate_hosts()
    assert hosts == ["fe80::1%15", "10.4.9.9", "10.4.9.10"]
    assert len(warnings) == 1 and "combined" in warnings[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"targets": "10.0.0.1"},
        {"targets": ["not-an-ip"]},
        {"networks": ["bad-prefix"]},
        {"max_hosts": 0},
        {"max_hosts": True},
        {"targets": ["10.0.0.1", "10.0.0.2"], "max_hosts": 1},
    ],
)
def test_invalid_discovery_configuration_is_rejected_before_network_io(kwargs):
    with pytest.raises(DroidockError):
        UnicastDiscovery(**kwargs)


def test_advertised_address_and_port_are_used_instead_of_assuming_packet_source():
    services = parse_unicast_services(response(), "10.4.9.1")
    assert services == [Service("opaque", ServiceKind.CONNECT, "10.4.9.12:41321", "unicast-mdns")]


def test_ipv6_link_local_address_keeps_receiving_interface_scope():
    services = parse_unicast_services(response(advertised="fe80::12"), "fe80::1%15")
    assert services[0].endpoint == "[fe80::12%15]:41321"


def test_srv_only_responder_and_malformed_packets():
    assert parse_unicast_services(response(source_only=True), "10.4.9.1")[0].endpoint == "10.4.9.1:41321"
    assert parse_unicast_services(b"not dns", "10.4.9.1") == []


def test_unicast_queries_only_planned_ips_and_closes_socket(monkeypatch):
    class Socket:
        def __init__(self):
            self.sent = []
            self.closed = False

        def setblocking(self, value):
            assert value is False

        def sendto(self, query, address):
            self.sent.append((dns.message.from_wire(query), address))

        def recvfrom(self, size):
            return response(source_only=True), ("10.4.9.1", 5353)

        def close(self):
            self.closed = True

    sock = Socket()
    monkeypatch.setattr("droidock.network_discovery.socket.socket", lambda *_: sock)
    events = iter([([sock], [], []), ([], [], [])])
    monkeypatch.setattr("droidock.network_discovery.select.select", lambda *_: next(events))
    services, warnings = UnicastDiscovery(["10.4.9.1"]).discover(0.5)
    assert services[0].endpoint == "10.4.9.1:41321"
    assert warnings == []
    assert len(sock.sent) == 3
    assert all(address == ("10.4.9.1", 5353) for _, address in sock.sent)
    assert sock.closed


def test_unicast_ipv6_uses_scope_id_and_ignores_unsolicited_packets(monkeypatch):
    calls = []

    class Socket:
        def setblocking(self, value):
            pass

        def sendto(self, query, address):
            calls.append(address)

        def recvfrom(self, size):
            return response(source_only=True), ("fe80::99", 5353, 0, 15)

        def close(self):
            pass

    sock = Socket()

    def create(family, kind):
        assert family == socket.AF_INET6 and kind == socket.SOCK_DGRAM
        return sock

    monkeypatch.setattr("droidock.network_discovery.socket.socket", create)
    events = iter([([sock], [], []), ([], [], [])])
    monkeypatch.setattr("droidock.network_discovery.select.select", lambda *_: next(events))
    services, _ = UnicastDiscovery(["fe80::1%15"]).discover(0.5)
    assert calls == [("fe80::1", 5353, 0, 15)] * 3
    assert services == []


def test_zero_budget_does_not_read_interfaces_or_open_sockets(monkeypatch):
    monkeypatch.setattr(
        "droidock.network_discovery.ifaddr.get_adapters", lambda: pytest.fail("Interface I/O")
    )
    monkeypatch.setattr("droidock.network_discovery.socket.socket", lambda *_: pytest.fail("Socket I/O"))
    assert UnicastDiscovery(include_local_networks=True).discover(0) == ([], [])


class Provider:
    def __init__(self, services=(), error=None):
        self.services = list(services)
        self.error = error
        self.budgets = []

    def discover(self, seconds):
        self.budgets.append(seconds)
        if self.error:
            raise self.error
        return self.services, []


def test_composite_retains_partial_results_and_reports_provider_failure():
    item = Service("one", ServiceKind.CONNECT, "10.0.0.1:40001")
    first, failed, last = Provider([item]), Provider(error=OSError("No interface")), Provider([item])
    services, warnings = CompositeDiscovery(first, failed, last).discover(0.3)
    assert services == [item]
    assert "No interface" in warnings[0]
    assert first.budgets == last.budgets == [0.3]


def test_composite_fallback_only_runs_when_needed_and_divides_budget():
    item = Service("one", ServiceKind.CONNECT, "10.0.0.1:40001")
    first, second, third = Provider(), Provider([item]), Provider()
    result, _ = CompositeDiscovery(first, second, third, fallback_only=True).discover(0.3)
    assert result == [item]
    assert 0 < first.budgets[0] <= 0.100001
    assert 0 < second.budgets[0] <= 0.150001
    assert third.budgets == []
