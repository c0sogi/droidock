from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import FakeBackend, FakeDiscovery, connected
from rich.console import Console
from typer.testing import CliRunner

from droidock import AdbBackend, ConnectionManager, DeviceStore, DroidockError, Settings
from droidock.cli import app
from droidock.interactive import InteractiveCli


class RestartBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.restarts = 0

    def restart_server(self):
        self.restarts += 1
        self.online.clear()


@pytest.fixture
def restart_rig(tmp_path):
    backend = RestartBackend()
    manager = ConnectionManager(DeviceStore(tmp_path), backend=backend, discovery=FakeDiscovery())
    manager.configure(discovery_seconds=0, connect_attempts=1, auto_connect_on_start=False)
    return manager, backend


def test_restart_reconnects_only_opted_in_profiles_and_keeps_partial_failures(restart_rig):
    manager, backend = restart_rig
    records = []
    for index in range(3):
        address = f"192.0.2.{index + 1}:40001"
        backend.online[address] = connected(address, serial=f"DEVICE-{index}", guid=f"guid-{index}")
        records.append(manager.register(address, name=f"Device {index}"))
    manager.update_device(records[2].id, auto_connect=False)
    backend.network["192.0.2.1:40001"] = backend.online["192.0.2.1:40001"]
    report = manager.restart_server()
    assert backend.restarts == 1
    assert report.connected == [records[0].id]
    assert list(report.errors) == [records[1].id]
    assert "192.0.2.3:40001" not in backend.connected
    state = manager.store.read()
    assert [(d.id, d.name, d.serial, d.endpoints) for d in state.devices] == [
        (d.id, d.name, d.serial, d.endpoints) for d in records
    ]
    assert state.default_device == records[0].id
    assert state.devices[2].auto_connect is False


def test_restart_can_skip_reconnection(restart_rig):
    manager, backend = restart_rig
    before = manager.store.path.read_bytes()
    with patch.object(manager, "auto_connect", side_effect=AssertionError("Unexpected reconnect")):
        report = manager.restart_server(reconnect=False)
    assert backend.restarts == 1
    assert not report.connected and not report.errors
    assert manager.store.path.read_bytes() == before


def test_failed_restart_does_not_connect_or_change_profiles(restart_rig):
    manager, backend = restart_rig
    before = manager.store.path.read_bytes()
    with (
        patch.object(backend, "restart_server", side_effect=DroidockError("cannot start")),
        patch.object(manager, "auto_connect") as reconnect,
        pytest.raises(DroidockError),
    ):
        manager.restart_server()
    reconnect.assert_not_called()
    assert manager.store.path.read_bytes() == before


def test_unsupported_backend_fails_without_side_effects(rig):
    manager, backend, _ = rig
    with pytest.raises(DroidockError) as error:
        manager.restart_server()
    assert error.value.code == "unsupported_operation"
    assert not backend.connected


def test_backend_restarts_only_selected_port_and_checks_server_afterward(monkeypatch):
    backend = AdbBackend(Settings(server_port=5041))
    backend._executable = Path("adb.exe")
    order = []
    for variable in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT"):
        monkeypatch.setenv(variable, "another-host")

    def execute(command, **kwargs):
        order.append(command[-1])
        assert command == ["adb.exe", "-P", "5041", command[-1]]
        assert kwargs["timeout"] >= 20
        assert not any(
            key in kwargs["env"]
            for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT")
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def check(*, require_running=False):
        assert order == ["kill-server", "start-server"]
        assert require_running
        order.append("verified")

    with (
        patch("droidock.adb.subprocess.run", side_effect=execute),
        patch.object(backend, "_check_server", side_effect=check),
    ):
        backend.restart_server()
    assert order == ["kill-server", "start-server", "verified"]


@pytest.mark.parametrize("failed", ["kill-server", "start-server"])
def test_restart_stops_at_failed_stage(failed):
    backend = AdbBackend()
    backend._executable = Path("adb.exe")

    def execute(command, **kwargs):
        if command[-1] == failed:
            raise subprocess.TimeoutExpired(command, 20)
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch("droidock.adb.subprocess.run", side_effect=execute) as run,
        patch.object(backend, "_check_server") as check,
        pytest.raises(DroidockError) as error,
    ):
        backend.restart_server()
    assert error.value.code == "server_restart_failed"
    assert failed in str(error.value)
    assert run.call_count == (1 if failed == "kill-server" else 2)
    check.assert_not_called()


def test_restart_does_not_report_success_when_server_is_still_absent():
    backend = AdbBackend()
    backend._executable = Path("adb.exe")
    with (
        patch("droidock.adb.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")),
        patch("droidock.adb.socket.create_connection", side_effect=ConnectionRefusedError),
        pytest.raises(DroidockError) as error,
    ):
        backend.restart_server()
    assert error.value.code == "server_unavailable"


def test_invalid_executable_is_checked_before_stopping_server(tmp_path):
    backend = AdbBackend(Settings(adb_path=str(tmp_path / "missing-adb")))
    with patch("droidock.adb.subprocess.run") as run, pytest.raises(DroidockError):
        backend.restart_server()
    run.assert_not_called()


@pytest.mark.parametrize("approve", [False, True])
def test_interactive_restart_confirmation_and_refresh(restart_rig, approve):
    manager, backend = restart_rig
    output = StringIO()
    cli = InteractiveCli(manager, plain=True, console=Console(file=output))
    with (
        patch.object(cli, "choose", side_effect=["restart", None]),
        patch.object(cli, "confirm", return_value=approve),
        patch.object(manager, "scan", wraps=manager.scan) as scan,
    ):
        cli.run()
    assert backend.restarts == int(approve)
    assert scan.call_count == (2 if approve else 1)
    assert "interrupts all apps" in output.getvalue()


def test_cli_json_requires_explicit_confirmation(restart_rig):
    manager, backend = restart_rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        denied = CliRunner().invoke(app, ["restart-server", "--json"])
        result = CliRunner().invoke(app, ["restart-server", "--yes", "--json"])
    assert denied.exit_code == 1
    assert json.loads(denied.output)["error"]["code"] == "confirmation_required"
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["server_restarted"] is True
    assert backend.restarts == 1


def test_cli_declined_restart_does_nothing(restart_rig):
    manager, backend = restart_rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, ["restart-server"], input="n\n")
    assert result.exit_code == 0
    assert backend.restarts == 0


def test_cli_reports_reconnect_failure_separately_from_successful_restart(restart_rig):
    manager, backend = restart_rig
    backend.online["192.0.2.1:40001"] = connected("192.0.2.1:40001")
    device = manager.register("192.0.2.1:40001", name="Saved device")
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, ["restart-server", "--yes", "--json"])
    report = json.loads(result.output)
    assert result.exit_code == 1
    assert report["server_restarted"] is True
    assert list(report["errors"]) == [device.id]


def test_unreadable_profiles_prevent_restart(restart_rig):
    manager, backend = restart_rig
    manager.store.path.write_text("broken", encoding="utf-8")
    with pytest.raises(DroidockError):
        manager.restart_server()
    assert backend.restarts == 0
