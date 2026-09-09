from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from unittest.mock import patch

import pytest
from conftest import connected
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from typer.testing import CliRunner

from droidock import DroidockError, __version__
from droidock.cli import app
from droidock.interactive import InteractiveCli, show_snapshot

runner = CliRunner()


def test_cli_and_library_share_persistent_profiles(rig):
    manager, _, _ = rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = runner.invoke(app, ["register", "USB-A", "--name", "My Android", "--json"])
        assert result.exit_code == 0, result.output
        identifier = json.loads(result.output)["id"]
        result = runner.invoke(app, ["profile", "My Android", "--no-auto-connect"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["devices", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["devices"][0]["id"] == identifier
        assert not manager.device(identifier).auto_connect


def test_plain_menu_can_register_and_exit(rig, monkeypatch):
    manager, _, _ = rig
    answers = iter(["3", "1", "1", "Office Android", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    InteractiveCli(manager, plain=True).run()
    assert manager.device().name == "Office Android"


def test_arrow_menu_back_and_exit_return_none(rig, monkeypatch):
    manager, _, _ = rig

    def select_back(*_, choices, **kwargs):
        class Answer:
            def ask(self):
                return choices[-1].value

        return Answer()

    monkeypatch.setattr("droidock.interactive.questionary.select", select_back)
    menu = InteractiveCli(manager)
    assert menu.choose("Menu", [("Do something", "action")]) is None
    assert menu.choose("Menu", [("Do something", "action")], back="Exit") is None
    assert not menu.confirm("Continue?")


def test_core_import_does_not_import_cli_or_prompt_packages():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import droidock, sys; assert 'typer' not in sys.modules; "
            "assert 'questionary' not in sys.modules; assert 'rich' not in sys.modules; "
            "assert 'droidock.cli' not in sys.modules; assert 'droidock.display' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_settings_validation_does_not_mutate_valid_state(rig):
    manager, _, _ = rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = runner.invoke(app, ["settings", "server_port", "99999"])
    assert result.exit_code == 1
    assert manager.settings.server_port == 5037


@pytest.mark.parametrize(
    ("answers", "expected_scans"),
    [
        (["6", "0", "2", "0", "3", "0", "0"], 1),
        (["6", "1", "6", "0", "0"], 1),
        (["1", "0"], 2),
    ],
    ids=["back-from-menus", "change-startup-setting", "explicit-scan"],
)
def test_navigation_only_scans_on_startup_or_explicit_request(rig, monkeypatch, answers, expected_scans):
    manager, _, _ = rig
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    with patch.object(manager, "scan", wraps=manager.scan) as scan:
        InteractiveCli(manager, plain=True).run()
    assert scan.call_count == expected_scans


def test_real_arrow_key_exit_does_not_scan_or_prompt_again(rig, monkeypatch):
    manager, _, _ = rig
    menu = InteractiveCli(manager)
    original_choose = menu.choose
    prompts = []

    def choose_once(*args, **kwargs):
        assert not prompts, "Exit opened another prompt"
        prompts.append(args[0])
        return original_choose(*args, **kwargs)

    monkeypatch.setattr(menu, "choose", choose_once)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with patch.object(manager, "scan", wraps=manager.scan) as scan:
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                pipe.send_text("\x1b[B" * 7 + "\r")
                menu.run()
    assert len(prompts) == 1
    assert scan.call_count == 1


def test_initial_discovery_failure_still_allows_exit(rig, monkeypatch):
    manager, _, _ = rig
    monkeypatch.setattr("builtins.input", lambda *_: "0")
    with patch.object(
        manager,
        "scan",
        side_effect=[DroidockError("Discovery unavailable"), AssertionError("Unexpected retry")],
    ) as scan:
        InteractiveCli(manager, plain=True).run()
    assert scan.call_count == 1


def test_profile_edits_update_the_menu_without_discovery(rig, monkeypatch):
    manager, _, _ = rig
    manager.register("USB-A", name="Original")
    manager.configure(auto_connect_on_start=False)
    responses = iter(["2", "1", "2", "Renamed", "2", "1", "3", "2", "1", "4", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    with patch.object(manager, "scan", wraps=manager.scan) as scan:
        with patch("droidock.interactive.show_snapshot", wraps=show_snapshot) as display:
            InteractiveCli(manager, plain=True).run()
    assert scan.call_count == 1
    assert display.call_args is not None
    visible = display.call_args.args[1].devices[0]
    assert visible.name == "Renamed"
    assert not visible.auto_connect
    assert display.call_args.args[2] == visible.id


def test_registration_refreshes_connections_before_returning_to_menu(rig, monkeypatch):
    manager, backend, _ = rig
    endpoint = "10.0.0.10:40001"
    backend.network[endpoint] = connected(endpoint)
    responses = iter(["3", "3", "1", endpoint, "Office Android", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    with patch("droidock.interactive.show_snapshot", wraps=show_snapshot) as display:
        InteractiveCli(manager, plain=True).run()
    assert display.call_args is not None
    snapshot = display.call_args.args[1]
    assert snapshot.devices[0].name == "Office Android"
    assert any(t.address == endpoint and t.ready for t in snapshot.transports)


def test_version_does_not_initialize_device_management():
    with patch("droidock.cli.ConnectionManager", side_effect=AssertionError("Unexpected device I/O")):
        result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"droidock {__version__}"


@pytest.mark.parametrize(
    ("arguments", "key"),
    [
        (["devices"], "devices"),
        (["register", "USB-A", "--name", "Office XR"], "serial"),
        (["connect", "Office XR"], "serial"),
        (["profile", "Office XR", "--no-auto-connect"], "serial"),
        (["settings"], "auto_connect_on_start"),
        (["diagnose"], "snapshot"),
        (["auto-connect"], "connected"),
        (["disconnect", "Office XR"], "disconnected"),
        (["pair", "10.0.0.10:40001", "--code-stdin"], "status"),
    ],
)
def test_result_commands_keep_machine_readable_json(rig, arguments, key):
    manager, _, _ = rig
    record = manager.register("USB-A", name="Office XR")
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = runner.invoke(app, [*arguments, "--json"], input="765432\n")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert key in data
    assert "765432" not in result.output
    if key == "serial":
        assert data["id"] == record.id
        assert data["serial"] == record.serial
    if arguments[0] == "profile":
        assert data["auto_connect"] is False


def test_default_settings_output_is_readable(rig):
    manager, _, _ = rig
    with patch("droidock.cli.ConnectionManager", return_value=manager):
        result = runner.invoke(app, ["settings", "auto_connect_on_start", "false"])
    assert result.exit_code == 0, result.output
    assert "Connection settings" in result.output
    assert "Disabled" in result.output
    assert "Profile storage" in result.output
    assert '"auto_connect_on_start"' not in result.output
    assert not manager.settings.auto_connect_on_start


def test_interactive_details_and_diagnostics_show_values_without_rescanning_on_exit(rig, monkeypatch):
    manager, backend, _ = rig
    record = manager.register("USB-A", name="[red]Office[/red]")
    manager.store.update(lambda state: state.devices[0].endpoints.append("[2001:db8::1]:40002"))
    manager.configure(auto_connect_on_start=False)
    monkeypatch.setattr(
        backend,
        "diagnostics",
        lambda: {"server": 'executable_absolute_path: "C:\\\\Android\\\\adb.exe"\nmdns_enabled: true'},
    )
    responses = iter(["2", "1", "5", "5", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    output = StringIO()
    with patch.object(manager, "scan", wraps=manager.scan) as scan:
        InteractiveCli(manager, plain=True, console=Console(file=output, width=80, color_system=None)).run()
    rendered = output.getvalue()
    assert "Device details" in rendered
    assert "Connection diagnostics" in rendered
    assert "[red]Office[/red]" in rendered
    assert "[2001:db8::1]:40002" in rendered
    assert "C:\\Android\\adb.exe" in rendered
    assert record.id in rendered
    assert '"serial":' not in rendered
    assert '"snapshot":' not in rendered
    assert scan.call_count == 2  # Initial discovery and the explicit diagnostic request only.


def test_json_reports_initialization_errors_without_overwriting_profiles(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"broken"', encoding="utf-8")
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "settings", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_store"
    assert path.read_text(encoding="utf-8") == '{"broken"'


@pytest.mark.parametrize("arguments", [["pair", "--json"], ["pair", "10.0.0.10:40001", "--json"]])
def test_json_pairing_requires_explicit_input_without_opening_a_prompt(arguments):
    with patch("droidock.cli.typer.prompt", side_effect=AssertionError("Unexpected prompt")):
        result = runner.invoke(app, arguments)
    assert result.exit_code == 1, result.output
    assert "--code-stdin" in json.loads(result.output)["error"]["message"]
