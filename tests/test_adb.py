from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from droidock import DroidockError, ServiceKind
from droidock.adb import AdbBackend, parse_services
from droidock.models import normalize_endpoint


@pytest.mark.parametrize(
    ("configured", "variables", "expected"),
    [
        (False, {"DROIDOCK_ADB_PATH": "current"}, "current"),
        (True, {"DROIDOCK_ADB_PATH": "current"}, "configured"),
    ],
)
def test_adb_override_precedence(tmp_path, monkeypatch, configured, variables, expected):
    monkeypatch.delenv("DROIDOCK_ADB_PATH", raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, str(tmp_path / value))
    expected_path = tmp_path / expected
    expected_path.write_bytes(b"")
    calls = []

    def version(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Android Debug Bridge version 1.0.41\n", "")

    monkeypatch.setattr(subprocess, "run", version)
    backend = AdbBackend()
    if configured:
        backend.settings.adb_path = str(expected_path)
    assert backend.executable == expected_path
    assert calls == [[str(expected_path), "version"]]


@pytest.mark.parametrize("value", ["192.168.1.1:40001", "phone.local:40002", "[::1]:40003"])
def test_explicit_endpoints(value):
    assert normalize_endpoint(value) == value


@pytest.mark.parametrize(
    "value", ["192.168.1.1", "host:0", "host:65536", "-x:40001", "host:40 & whoami", "::1:40001"]
)
def test_invalid_endpoint_is_rejected(value):
    with pytest.raises(DroidockError):
        normalize_endpoint(value)


def test_discovery_distinguishes_pairing_and_connect_ports():
    services = parse_services("""List of discovered mdns services
adb-a _adb-tls-pairing._tcp. 192.168.1.1:40001
adb-a _adb-tls-connect._tcp 192.168.1.1:40002
adb-legacy _adb._tcp 192.168.1.1:5555
unrelated _other._tcp 192.168.1.1:40003
unknown-adb _adb-tls-unknown._tcp 192.168.1.1:40004
""")
    assert len(services) == 3
    assert services[0].kind is ServiceKind.PAIRING
    assert services[1].kind is ServiceKind.CONNECT
    assert services[2].kind is ServiceKind.CONNECT


def prepared_backend(monkeypatch):
    backend = AdbBackend()
    backend._executable = Path("adb.exe")
    monkeypatch.setattr(backend, "_check_server", lambda: None)
    return backend


def test_pairing_code_goes_through_stdin_and_is_redacted(monkeypatch):
    backend = prepared_backend(monkeypatch)
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 1, "incorrect code 765432", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DroidockError) as error:
        backend.pair("10.0.0.1:40001", "765432")
    assert "765432" not in str(error.value)
    assert "765432" not in observed["command"]
    assert observed["input"] == "765432\n"


def test_exit_zero_connection_failure_is_not_success(monkeypatch):
    backend = prepared_backend(monkeypatch)
    monkeypatch.setattr(backend, "_run", lambda *a, **k: "failed to connect: connection refused")
    with pytest.raises(DroidockError):
        backend.connect("10.0.0.1:40001")


def test_timeout_is_classified(monkeypatch):
    backend = prepared_backend(monkeypatch)

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DroidockError) as error:
        backend.inspect("USB-123")
    assert error.value.code == "timeout"


def test_boot_serial_fallback_when_regular_serial_is_unknown(monkeypatch):
    backend = prepared_backend(monkeypatch)
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *a, **k: (
            "[ro.serialno]: [unknown]\n[ro.boot.serialno]: [REAL-SERIAL]\n[ro.product.model]: [Phone]\n"
        ),
    )
    identity = backend.inspect("USB").identity
    assert identity is not None
    assert identity.serial == "REAL-SERIAL"


def test_mdns_transport_can_disconnect_without_touching_other_devices(monkeypatch):
    backend = prepared_backend(monkeypatch)
    calls = []
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: calls.append(args))
    address = "adb-SERIAL-A-random._adb-tls-connect._tcp"
    backend.disconnect(address)
    assert calls == [("disconnect", address)]


def test_incompatible_existing_server_is_not_restarted(monkeypatch):
    class Socket:
        def __init__(self):
            self.response = bytearray(b"OKAY00040028")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def sendall(self, data):
            assert data == b"000chost:version"

        def recv(self, count):
            result = bytes(self.response[:count])
            del self.response[:count]
            return result

    import socket

    backend = AdbBackend()
    backend._executable = Path("adb.exe")
    backend._protocol = 41
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Socket())
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(DroidockError) as error:
        backend.transports()
    assert error.value.code == "server_conflict"
    assert calls == []
