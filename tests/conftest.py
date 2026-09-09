from __future__ import annotations

import pytest

from droidock import (
    ConnectionManager,
    DeviceStore,
    DroidockError,
    Identity,
    PairResult,
    Service,
    Transport,
)


@pytest.fixture(autouse=True)
def isolate_configuration_environment(monkeypatch):
    for name in ("HOME", "ADB_PATH", "TAILSCALE_PATH"):
        monkeypatch.delenv(f"DROIDOCK_{name}", raising=False)


class FakeBackend:
    def __init__(self):
        self.online: dict[str, Transport] = {}
        self.network: dict[str, Transport] = {}
        self.advertisements: list[Service] = []
        self.connected: list[str] = []
        self.disconnected: list[str] = []

    def transports(self):
        return list(self.online.values())

    def inspect(self, address):
        if address not in self.online:
            raise DroidockError("not responding", code="timeout")
        return self.online[address]

    def connect(self, endpoint):
        self.connected.append(endpoint)
        if endpoint not in self.network:
            raise DroidockError("connection refused")
        self.online[endpoint] = self.network[endpoint]

    def pair(self, endpoint, code):
        return PairResult(endpoint, "paired-guid")

    def disconnect(self, endpoint):
        self.disconnected.append(endpoint)
        self.online.pop(endpoint, None)

    def reconnect(self, address):
        pass

    def services(self):
        return list(self.advertisements)

    def diagnostics(self):
        return {"version": "test"}


class FakeDiscovery:
    def __init__(self):
        self.services = []

    def discover(self, seconds):
        return self.services, []


def connected(address="USB-A", serial="SERIAL-A", model="Phone", guid="wifi-guid-a"):
    return Transport(address, "device", Identity(serial, "Maker", model, guid))


@pytest.fixture
def rig(tmp_path):
    backend = FakeBackend()
    discovery = FakeDiscovery()
    store = DeviceStore(tmp_path / "profiles")
    manager = ConnectionManager(store, backend=backend, discovery=discovery)
    manager.configure(discovery_seconds=0.0, connect_attempts=1)
    backend.online["USB-A"] = connected()
    return manager, backend, discovery
