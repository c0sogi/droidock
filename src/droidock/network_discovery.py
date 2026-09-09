"""Optional bounded unicast discovery on explicit hosts or actual interface networks."""

from __future__ import annotations

import ipaddress
import math
import select
import socket
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

import dns.exception
import dns.message
import dns.rdatatype
import ifaddr

from .errors import DroidockError
from .interfaces import Discovery
from .models import ADB_SERVICE_KINDS, Service, normalize_endpoint


class CompositeDiscovery:
    """Combine providers within a shared time budget, retaining partial results.

    Providers must honor the Discovery.discover(seconds) time budget. In fallback
    mode each provider receives a share of the remaining budget, until results arrive.
    """

    def __init__(self, *providers: Discovery, fallback_only: bool = False) -> None:
        self.providers = providers
        self.fallback_only = fallback_only

    @staticmethod
    def _call(provider: Discovery, seconds: float) -> tuple[list[Service], list[str]]:
        try:
            return provider.discover(seconds)
        except (OSError, DroidockError) as exc:
            return [], [f"{type(provider).__name__}: {exc}"]

    def discover(self, seconds: float) -> tuple[list[Service], list[str]]:
        if not math.isfinite(seconds) or seconds < 0:
            raise DroidockError("Discovery time must be finite and nonnegative.", code="invalid_setting")
        if seconds == 0 or not self.providers:
            return [], []
        results: list[tuple[list[Service], list[str]]] = []
        if self.fallback_only:
            deadline = time.monotonic() + seconds
            for index, provider in enumerate(self.providers):
                budget = max(0.0, deadline - time.monotonic()) / (len(self.providers) - index)
                result = self._call(provider, budget)
                results.append(result)
                if result[0]:
                    break
        else:
            with ThreadPoolExecutor(max_workers=len(self.providers)) as executor:
                futures = [executor.submit(self._call, provider, seconds) for provider in self.providers]
                results = [future.result() for future in futures]
        services = {(s.instance, s.kind, s.endpoint): s for batch, _ in results for s in batch}
        warnings = list(dict.fromkeys(w for _, batch in results for w in batch))
        return sorted(services.values(), key=lambda s: (s.kind, s.instance, s.endpoint)), warnings


def local_ipv4_networks() -> tuple[list[str], set[str]]:
    """Read real interface prefixes; do not infer a /24 from an interface address."""
    networks: set[str] = set()
    own: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for entry in adapter.ips:
            if not isinstance(entry.ip, str):
                continue  # IPv6 uses multicast or explicit hosts, never subnet enumeration.
            address = ipaddress.ip_address(entry.ip)
            own.add(str(address))
            if address.is_loopback or address.is_unspecified:
                continue
            networks.add(str(ipaddress.ip_network(f"{address}/{entry.network_prefix}", strict=False)))
    return sorted(networks), own


def parse_unicast_services(payload: bytes, source: str) -> list[Service]:
    """Read ADB SRV records and their advertised addresses; names never prove identity."""
    try:
        response = dns.message.from_wire(payload)
        ipaddress.ip_address(source)
    except (dns.exception.DNSException, ValueError):
        return []
    addresses: dict[str, list[str]] = {}
    records = [*response.answer, *response.additional]
    for rrset in records:
        if rrset.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
            addresses.setdefault(rrset.name.to_text().casefold(), []).extend(r.to_text() for r in rrset)
    services: set[Service] = set()
    for rrset in records:
        if rrset.rdtype != dns.rdatatype.SRV:
            continue
        owner = rrset.name.to_text()
        for service_type, kind in ADB_SERVICE_KINDS.items():
            suffix = f".{service_type}.local."
            if not owner.casefold().endswith(suffix):
                continue
            for record in rrset:
                port = getattr(record, "port", 0)
                target = str(getattr(record, "target", "")).casefold()
                if not isinstance(port, int) or not 1 <= port <= 65535:
                    continue
                for host in addresses.get(target, [source]):
                    # Preserve the receiving interface for link-local IPv6 advertisements.
                    if (
                        ":" in host
                        and "%" not in host
                        and "%" in source
                        and ipaddress.ip_address(host).is_link_local
                    ):
                        host += "%" + source.split("%", 1)[1]
                    rendered = f"[{host}]" if ":" in host else host
                    try:
                        endpoint = normalize_endpoint(f"{rendered}:{port}")
                    except DroidockError:
                        continue
                    services.add(Service(owner[: -len(suffix)], kind, endpoint, "unicast-mdns"))
    return sorted(services, key=lambda s: (s.kind, s.instance, s.endpoint))


class UnicastDiscovery:
    """Query UDP 5353 on explicit IPs/CIDRs or actual local IPv4 networks.

    Networks exceeding max_hosts are skipped with a warning, never truncated or
    expanded to an assumed prefix. IPv6 supports explicit addresses (including scopes).
    No TCP port scan or implicit Tailscale range scan is performed.
    """

    def __init__(
        self,
        targets: Iterable[str] = (),
        *,
        networks: Iterable[str] = (),
        include_local_networks: bool = False,
        max_hosts: int = 256,
    ) -> None:
        if type(max_hosts) is not int or not 1 <= max_hosts <= 65536:
            raise DroidockError("max_hosts must be between 1 and 65536.", code="invalid_setting")
        if isinstance(targets, str) or isinstance(networks, str):
            raise DroidockError(
                "Pass targets and networks as collections, not single strings.", code="invalid_setting"
            )
        try:
            self.targets = tuple(dict.fromkeys(str(ipaddress.ip_address(host)) for host in targets))
            self.networks = tuple(str(ipaddress.ip_network(value, strict=False)) for value in networks)
        except ValueError as exc:
            raise DroidockError("Invalid discovery IP address or network.", code="invalid_setting") from exc
        if len(self.targets) > max_hosts:
            raise DroidockError("Explicit discovery targets exceed max_hosts.", code="invalid_setting")
        self.include_local_networks = include_local_networks
        self.max_hosts = max_hosts

    def candidate_hosts(self) -> tuple[list[str], list[str]]:
        """Build a bounded plan without sending packets; return skipped-network warnings."""
        hosts = list(self.targets)
        networks = list(self.networks)
        own: set[str] = set()
        warnings: list[str] = []
        if self.include_local_networks:
            try:
                local, own = local_ipv4_networks()
                networks.extend(local)
            except OSError as exc:
                warnings.append(f"Cannot read network interfaces: {exc}")
        for value in dict.fromkeys(networks):
            network = ipaddress.ip_network(value)
            if network.version == 6:
                warnings.append(
                    f"IPv6 subnet enumeration is disabled: {network}. Use multicast or explicit IPs."
                )
                continue
            size = network.num_addresses if network.prefixlen >= 31 else network.num_addresses - 2
            if size > self.max_hosts:
                warnings.append(f"Skipped {network}: {size} hosts exceed max_hosts={self.max_hosts}.")
                continue
            extra = [str(host) for host in network.hosts() if str(host) not in own and str(host) not in hosts]
            if len(hosts) + len(extra) > self.max_hosts:
                warnings.append(f"Skipped {network}: combined targets exceed max_hosts={self.max_hosts}.")
                continue
            hosts.extend(extra)
        return hosts, warnings

    def discover(self, seconds: float) -> tuple[list[Service], list[str]]:
        if not math.isfinite(seconds) or seconds < 0:
            raise DroidockError("Discovery time must be finite and nonnegative.", code="invalid_setting")
        if seconds == 0:
            return [], []
        deadline = time.monotonic() + seconds
        hosts, warnings = self.candidate_hosts()
        if not hosts:
            return [], warnings
        queries = [
            dns.message.make_query(f"{kind}.local.", dns.rdatatype.PTR).to_wire()
            for kind in ADB_SERVICE_KINDS
        ]
        sockets: dict[int, socket.socket] = {}
        allowed = {host.split("%", 1)[0] for host in hosts}
        services: set[Service] = set()
        try:
            for host in hosts:
                if time.monotonic() >= deadline:
                    warnings.append("Discovery time expired before all targets were queried.")
                    break
                family = socket.AF_INET6 if ":" in host else socket.AF_INET
                try:
                    if family not in sockets:
                        sock = socket.socket(family, socket.SOCK_DGRAM)
                        sock.setblocking(False)
                        sockets[family] = sock
                    sock = sockets[family]
                    base, _, scope = host.partition("%")
                    scope_id = int(scope) if scope.isdigit() else socket.if_nametoindex(scope) if scope else 0
                    address = (base, 5353, 0, scope_id) if family == socket.AF_INET6 else (base, 5353)
                    for query in queries:
                        sock.sendto(query, address)
                except OSError as exc:
                    warnings.append(f"Cannot query {host}: {exc}")
            while sockets and (remaining := deadline - time.monotonic()) > 0:
                readable, _, _ = select.select(list(sockets.values()), [], [], remaining)
                if not readable:
                    break
                for sock in readable:
                    try:
                        payload, remote = sock.recvfrom(9000)
                    except (BlockingIOError, OSError):
                        continue
                    source = remote[0]
                    if source not in allowed:
                        continue
                    if len(remote) == 4 and remote[3]:
                        source += f"%{remote[3]}"
                    services.update(parse_unicast_services(payload, source))
        except OSError as exc:
            warnings.append(f"Unicast discovery unavailable: {exc}")
        finally:
            for sock in sockets.values():
                sock.close()
        return sorted(services, key=lambda s: (s.kind, s.instance, s.endpoint)), list(dict.fromkeys(warnings))
