from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from conftest import connected
from rich.console import Console

from droidock import (
    AdbPortScanner,
    DroidockError,
    PortScanProgress,
    PortScanStatus,
    TailscaleClient,
    TailscalePeer,
)
from droidock.display import AdbScanDisplay, show_tailscale_peers
from droidock.interactive import InteractiveCli

PEER = TailscalePeer(
    "node-xr",
    "[red]Office XR[/red]",
    "xr.example.ts.net",
    "android",
    ("100.64.0.10", "fd7a:115c:a1e0::10"),
    True,
    (1,),
)


def menu(rig, monkeypatch, answers):
    manager, _, _ = rig
    manager.configure(auto_connect_on_start=False)
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    output = StringIO()
    client = TailscaleClient()
    scanner = AdbPortScanner()
    ui = InteractiveCli(
        manager,
        plain=True,
        console=Console(file=output, width=120, color_system=None),
        tailscale=client,
        port_scanner=scanner,
    )
    return ui, output


def test_tailscale_navigation_and_exit_do_not_start_port_searches(rig, monkeypatch):
    ui, _ = menu(rig, monkeypatch, ["7", "0", "7", "1", "0", "0", "0"])
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]) as peers:
        with patch.object(ui.port_scanner, "scan", side_effect=AssertionError("Unexpected port scan")):
            with patch.object(ui.manager, "scan", wraps=ui.manager.scan) as scan:
                ui.run()
    assert peers.call_count == 1
    assert scan.call_count == 1


def test_only_explicit_refresh_updates_tailscale_list(rig, monkeypatch):
    ui, _ = menu(rig, monkeypatch, ["7", "2", "0", "0"])
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]) as peers:
        with patch.object(ui.port_scanner, "scan", side_effect=AssertionError("Unexpected port scan")):
            ui.run()
    assert peers.call_count == 2


def test_main_menu_never_requires_tailscale(rig, monkeypatch):
    ui, _ = menu(rig, monkeypatch, ["0"])
    with patch.object(ui.tailscale_client, "peers", side_effect=AssertionError("Unexpected Tailscale call")):
        ui.run()


def test_tailscale_error_still_leaves_main_menu_exit_available(rig, monkeypatch):
    ui, output = menu(rig, monkeypatch, ["7", "0"])
    with patch.object(
        ui.tailscale_client,
        "peers",
        side_effect=DroidockError("Install Tailscale", code="tailscale_missing"),
    ):
        ui.run()
    assert "Install Tailscale" in output.getvalue()


def test_explicit_search_connects_only_selected_peer_and_preserves_alias(rig, monkeypatch):
    manager, backend, _ = rig
    record = manager.register("USB-A", name="My saved XR")
    endpoint = "100.64.0.10:40001"
    backend.network[endpoint] = connected(endpoint)
    backend.online[endpoint] = connected(endpoint)
    ui, output = menu(rig, monkeypatch, ["7", "1", "1", "2", "", "0", "0"])
    calls = []

    async def scan(host, **kwargs):
        calls.append((host, kwargs["preferred_ports"], kwargs["excluded_ports"]))
        kwargs["on_progress"](PortScanProgress(host, 100, 1000, 10))
        result = PortScanProgress(host, 101, 1000, 10.1, PortScanStatus.FOUND, endpoint)
        kwargs["on_progress"](result)
        return result

    monkeypatch.setattr(ui.port_scanner, "scan", scan)
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]):
        ui.run()
    assert calls == [(PEER.addresses[0], (40001,), (1,))]
    assert backend.connected == [endpoint]
    assert len(manager.store.read().devices) == 1
    assert manager.device().id == record.id
    assert manager.device().name == "My saved XR"
    assert endpoint in manager.device().endpoints
    assert "ADB found" in output.getvalue()
    assert "ETA" in output.getvalue()


def test_keyboard_interrupt_cancels_search_and_returns_to_peer_menu(rig, monkeypatch):
    ui, output = menu(rig, monkeypatch, ["7", "1", "1", "0", "0", "0"])

    async def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui.port_scanner, "scan", cancel)
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]):
        with patch.object(ui.manager, "scan", wraps=ui.manager.scan) as scan:
            ui.run()
    assert scan.call_count == 1
    assert "Search cancelled. Returning to the device menu." in output.getvalue()
    assert not ui.manager.store.read().devices


def test_manual_port_can_use_the_peers_ipv6_address_without_scanning(rig, monkeypatch):
    manager, backend, _ = rig
    endpoint = f"[{PEER.addresses[1]}]:40001"
    backend.network[endpoint] = connected(endpoint)
    ui, _ = menu(rig, monkeypatch, ["7", "1", "3", "2", "2", "40001", "2", "Office XR", "0", "0"])
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]):
        with patch.object(ui.port_scanner, "scan", side_effect=AssertionError("Unexpected scan")):
            ui.run()
    assert backend.connected == [endpoint]
    assert manager.device().name == "Office XR"


def test_a_different_device_at_saved_tailscale_address_does_not_replace_profile(rig, monkeypatch):
    manager, backend, _ = rig
    record = manager.register("USB-A", name="My XR")
    manager.store.update(lambda s: s.devices[0].endpoints.append("100.64.0.10:40000"))
    backend.network["100.64.0.10:40001"] = connected("100.64.0.10:40001", serial="OTHER-DEVICE")
    ui, output = menu(rig, monkeypatch, ["7", "1", "2", "40001", "1", "0", "0", "0"])
    with patch.object(ui.tailscale_client, "peers", return_value=[PEER]):
        ui.run()
    assert len(manager.store.read().devices) == 1
    assert manager.device().id == record.id
    assert manager.device().endpoints == ["100.64.0.10:40000"]
    assert "does not match My XR" in output.getvalue()


def test_peer_table_is_one_row_for_two_addresses_and_renders_names_literally():
    output = StringIO()
    show_tailscale_peers(Console(file=output, width=100, color_system=None), [PEER])
    rendered = output.getvalue()
    assert rendered.count(PEER.name) == 1
    assert all(address in rendered for address in PEER.addresses)


def test_progress_renders_eta_and_retains_partial_count_on_early_success(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    output = StringIO()
    display = AdbScanDisplay(Console(file=output, width=120, color_system=None, force_terminal=True))
    with display.progress:
        display.update(PortScanProgress("100.64.0.10", 100, 1000, 10))
        display.update(
            PortScanProgress("100.64.0.10", 101, 1000, 10.1, PortScanStatus.FOUND, "100.64.0.10:40001")
        )
    rendered = output.getvalue()
    assert "ETA 01:30" in rendered
    assert "Elapsed 00:10" in rendered
    assert "101/1000" in rendered
    assert "ADB found" in rendered


def test_noninteractive_output_reports_progress_periodically():
    output = StringIO()
    display = AdbScanDisplay(Console(file=output, width=120, color_system=None, force_terminal=False))
    with display.progress:
        display.update(PortScanProgress("100.64.0.10", 0, 1000, 0))
        display.update(PortScanProgress("100.64.0.10", 100, 1000, 10))
        display.update(PortScanProgress("100.64.0.10", 101, 1000, 10.1))
    rendered = output.getvalue()
    assert "ETA estimating..." in rendered
    assert "ETA 01:30" in rendered
    assert rendered.count("100/1000") == 1
