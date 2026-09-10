from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from droidock import DroidockError
from droidock.dashboard import DashboardAction, DeviceEntry
from droidock.interactive import InteractiveCli


@pytest.fixture(autouse=True)
def application_session():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        yield


def screen_menu(manager, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    output = StringIO()
    console = Console(file=output, force_terminal=True, legacy_windows=False, width=120, height=45)
    return InteractiveCli(manager, console=console), output


def test_fullscreen_refresh_replaces_previous_page_and_exit_restores_terminal(rig, monkeypatch):
    manager, _, _ = rig
    menu, output = screen_menu(manager, monkeypatch)
    with (
        patch("droidock.dashboard.Dashboard.run", side_effect=[DashboardAction("scan"), None]),
        patch.object(menu, "text", side_effect=AssertionError("No pause needed for Scan or Exit")),
        patch.object(manager, "scan", wraps=manager.scan) as scan,
    ):
        menu.run()
    rendered = output.getvalue()
    assert "\x1b[?1049h" in rendered and "\x1b[?1049l" in rendered
    assert rendered.count("\x1b[2J") >= 3
    assert scan.call_count == 2
    assert not menu._fullscreen


def test_scan_error_remains_until_acknowledged_then_menu_is_clean(rig, monkeypatch):
    manager, _, _ = rig
    menu, output = screen_menu(manager, monkeypatch)
    initial = manager.scan()

    def acknowledge(*args, **kwargs):
        assert "Discovery failed for this test" in output.getvalue().split("\x1b[2J")[-1]
        return ""

    with (
        patch("droidock.dashboard.Dashboard.run", side_effect=[DashboardAction("scan"), None]),
        patch.object(manager, "scan", side_effect=[initial, DroidockError("Discovery failed for this test")]),
        patch.object(menu, "text", side_effect=acknowledge) as pause,
    ):
        menu.run()
    pause.assert_called_once_with("Press Enter to continue")
    assert "Discovery failed for this test" not in output.getvalue().split("\x1b[2J")[-1]


def test_details_wait_for_enter_without_scanning_when_returning(rig, monkeypatch):
    manager, _, _ = rig
    record = manager.register("USB-A", name="Office phone")
    manager.configure(auto_connect_on_start=False)
    menu, output = screen_menu(manager, monkeypatch)

    def acknowledge(*args, **kwargs):
        assert "Device details" in output.getvalue().split("\x1b[2J")[-1]
        return ""

    with (
        patch(
            "droidock.dashboard.Dashboard.run",
            side_effect=[
                DashboardAction("device", device=DeviceEntry(record, ())),
                None,
            ],
        ),
        patch.object(menu, "choose", return_value="details"),
        patch.object(menu, "text", side_effect=acknowledge) as pause,
        patch.object(manager, "scan", wraps=manager.scan) as scan,
    ):
        menu.run()
    pause.assert_called_once()
    assert scan.call_count == 1
    assert "Device details" not in output.getvalue().split("\x1b[2J")[-1]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_terminal_restored_on_interrupt_or_unexpected_error(rig, monkeypatch, failure):
    manager, _, _ = rig
    menu, output = screen_menu(manager, monkeypatch)
    with patch("droidock.dashboard.Dashboard.run", side_effect=failure), pytest.raises(failure):
        menu.run()
    assert "\x1b[?1049l" in output.getvalue()
    assert not menu._fullscreen


def test_plain_menu_keeps_transcript_without_new_acknowledgements(rig, monkeypatch):
    manager, _, _ = rig
    output = StringIO()
    menu = InteractiveCli(manager, plain=True, console=Console(file=output))
    with (
        patch.object(menu, "choose", side_effect=["diagnose", None]),
        patch.object(menu, "text", side_effect=AssertionError("Plain mode must not add pauses")),
    ):
        menu.run()
    assert "Connection diagnostics" in output.getvalue()
    assert "\x1b[?1049" not in output.getvalue() and "\x1b[2J" not in output.getvalue()
