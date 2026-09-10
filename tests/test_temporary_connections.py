from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest
from conftest import connected
from rich.console import Console
from typer.testing import CliRunner

from droidock import IdentityError, Service, ServiceGroup, ServiceKind, Transport
from droidock.cli import app
from droidock.interactive import InteractiveCli

ADDRESS = "192.0.2.10:40001"


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("method", ["connect_endpoint", "connect_endpoints"])
def test_temporary_api_leaves_profile_file_identical(rig, saved, method):
    manager, backend, _ = rig
    expected = manager.register("USB-A", name="Existing alias") if saved else None
    before = manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns
    backend.network[ADDRESS] = connected(ADDRESS)
    result = getattr(manager, method)(
        ADDRESS if method == "connect_endpoint" else [ADDRESS], remember=False, expected=expected
    )
    assert isinstance(result, Transport) and result.address == ADDRESS
    assert (manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns) == before


def test_temporary_api_still_checks_expected_identity(rig):
    manager, backend, _ = rig
    expected = manager.register("USB-A")
    backend.network[ADDRESS] = connected(ADDRESS, serial="OTHER")
    before = manager.store.path.read_bytes()
    with pytest.raises(IdentityError):
        manager.connect_endpoints([ADDRESS], remember=False, expected=expected)
    assert manager.store.path.read_bytes() == before


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_no_save_preserves_saved_alias_and_endpoints(rig, json_output):
    manager, backend, _ = rig
    record = manager.register("USB-A", name="Existing alias")
    before = manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns
    backend.network[ADDRESS] = connected(ADDRESS)
    arguments = ["connect", record.id, "--endpoint", ADDRESS, "--no-save"]
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, arguments + (["--json"] if json_output else []))
    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["saved"] is False and payload["address"] == ADDRESS
        assert payload["identity"]["serial"] == record.serial
    else:
        assert "Connected without saving" in result.output
    assert (manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns) == before


def test_cli_rejects_name_with_no_save_before_connecting(rig):
    manager, backend, _ = rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = CliRunner().invoke(app, ["connect", "--endpoint", ADDRESS, "--no-save", "--name", "Phone"])
    assert result.exit_code != 0
    assert "--name requires --save" in result.output
    assert not backend.connected


@pytest.mark.parametrize("choice", ["once", "connect", None])
def test_service_menu_connects_temporarily_saves_or_cancels(rig, choice):
    manager, backend, _ = rig
    backend.network[ADDRESS] = connected(ADDRESS)
    before = manager.store.path.read_bytes()
    output = StringIO()
    menu = InteractiveCli(manager, console=Console(file=output))
    with (
        patch.object(menu, "choose", return_value=choice),
        patch.object(menu, "text", return_value="Phone") as name,
    ):
        menu._service_action(ServiceGroup("selected", ServiceKind.CONNECT, (ADDRESS,)))
    if choice == "connect":
        assert manager.device().name == "Phone"
        name.assert_called_once()
    else:
        assert manager.store.path.read_bytes() == before
        name.assert_not_called()
    assert backend.connected == ([] if choice is None else [ADDRESS])
    if choice == "once":
        assert ADDRESS in [t.address for t in menu._snapshot.transports]
        assert "Connected without saving" in output.getvalue()
        # Explicit registration later still works.
        assert manager.register(ADDRESS, name="Saved later").name == "Saved later"


def test_temporary_service_refresh_does_not_update_existing_profiles(rig):
    manager, backend, discovery = rig
    manager.register("USB-A", name="Existing alias")
    before = manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns
    backend.network[ADDRESS] = connected(ADDRESS)
    discovery.services = [Service("selected", ServiceKind.CONNECT, ADDRESS)]
    menu = InteractiveCli(manager, console=Console(file=StringIO()))
    with patch.object(menu, "choose", return_value="once"):
        menu._service_action(ServiceGroup("selected", ServiceKind.CONNECT, ("192.0.2.10:39999",)))
    assert backend.connected == ["192.0.2.10:39999", ADDRESS]
    assert (manager.store.path.read_bytes(), manager.store.path.stat().st_mtime_ns) == before


def test_pairing_can_connect_without_profile_registration(rig):
    manager, backend, discovery = rig
    discovery.services = [Service("paired-guid", ServiceKind.CONNECT, ADDRESS)]
    backend.network[ADDRESS] = connected(ADDRESS)
    before = manager.store.path.read_bytes()
    menu = InteractiveCli(manager, console=Console(file=StringIO()))
    with (
        patch.object(menu, "choose", return_value="once"),
        patch.object(menu, "text", return_value="123456") as text,
    ):
        menu.pair(ServiceGroup("pair", ServiceKind.PAIRING, ("192.0.2.10:30000",)))
    assert backend.connected == [ADDRESS]
    text.assert_called_once_with("Six-digit code shown on the device", secret=True)
    assert manager.store.path.read_bytes() == before
