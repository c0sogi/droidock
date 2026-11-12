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
def isolate_configuration_environment(monkeypatch, tmp_path):
    for name in ("HOME", "ADB_PATH", "TAILSCALE_PATH"):
        monkeypatch.delenv(f"DROIDOCK_{name}", raising=False)
    # Clearing DROIDOCK_HOME must never fall back to the developer's real profiles.
    monkeypatch.setattr(
        "droidock.store.user_config_path", lambda *args, **kwargs: tmp_path / "default-profiles"
    )


class NoLinkProbe:
    """Stands in for the socket probe so tests never reach a real ADB server."""

    def __init__(self, *args, **kwargs):
        pass

    def round_trip(self, timeout=2.5):
        raise DroidockError("No device link in tests.", code="timeout")

    def throughput(self, *, budget=2.0):
        return None


@pytest.fixture(autouse=True)
def isolate_mirroring(monkeypatch, tmp_path):
    """Tests never download or start scrcpy, use one from PATH, probe a server, or move windows."""
    monkeypatch.setenv("DROIDOCK_SCRCPY_HOME", str(tmp_path / "scrcpy-cache"))
    monkeypatch.delenv("DROIDOCK_SCRCPY_PATH", raising=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected scrcpy download or launch")

    monkeypatch.setattr("droidock.scrcpy.open_url", unexpected)
    monkeypatch.setattr("droidock.scrcpy.find_on_path", lambda name: None)
    monkeypatch.setattr("droidock.mirror.launch_scrcpy", unexpected)
    monkeypatch.setattr("droidock.mirror.window_keeper", lambda: None)
    monkeypatch.setattr("droidock.mirror.AdbLinkProbe", NoLinkProbe)


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
