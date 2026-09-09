from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from droidock import DroidockError, TailscaleClient
from droidock.tailscale import parse_tailscale_status


def status(**peer_changes):
    peer = {
        "ID": "node-android",
        "HostName": "Office XR",
        "DNSName": "office-xr.example.ts.net.",
        "OS": "android",
        "Online": True,
        "TailscaleIPs": ["fd7a:115c:a1e0::10", "100.64.0.10", "100.64.0.10"],
        "PeerAPIURL": ["http://100.64.0.10:1", "http://[fd7a:115c:a1e0::10]:1", "http://100.64.0.99:123"],
        **peer_changes,
    }
    return json.dumps({"BackendState": "Running", "Peer": {"node-key": peer}, "NewField": "ignored"})


def test_peer_list_groups_addresses_and_records_only_its_peer_api_ports():
    peers = parse_tailscale_status(status())
    assert len(peers) == 1
    peer = peers[0]
    assert peer.id == "node-android"
    assert peer.name == "Office XR"
    assert peer.dns_name == "office-xr.example.ts.net"
    assert peer.addresses == ("100.64.0.10", "fd7a:115c:a1e0::10")
    assert peer.peer_api_ports == (1,)
    assert peer.online


def test_peer_parser_handles_optional_fields_and_invalid_addresses():
    peers = parse_tailscale_status(
        status(
            ID=None,
            HostName=None,
            DNSName=None,
            OS=None,
            Online="true",
            TailscaleIPs=["invalid", 123, None, "0.0.0.0", "224.0.0.251", "100.64.0.10"],
            PeerAPIURL=[None, "http://[invalid", "http://100.64.0.10:99999"],
        )
    )
    assert peers[0].name == "100.64.0.10"
    assert peers[0].id == "node-key"
    assert peers[0].os == "unknown"
    assert not peers[0].online
    assert peers[0].peer_api_ports == ()
    assert parse_tailscale_status(status(TailscaleIPs=None)) == []
    assert parse_tailscale_status('{"BackendState":"Running","Peer":null}') == []


@pytest.mark.parametrize("text", ["{", "[]", '{"BackendState":"Running","Peer":[1]}'])
def test_malformed_status_is_a_recoverable_error(text):
    with pytest.raises(DroidockError) as error:
        parse_tailscale_status(text)
    assert error.value.code == "tailscale_status_invalid"


@pytest.mark.parametrize("state", ["Stopped", "NeedsLogin", "Starting", None])
def test_inactive_tailscale_explains_how_to_continue(state):
    with pytest.raises(DroidockError, match="Start Tailscale and sign in") as error:
        parse_tailscale_status(json.dumps({"BackendState": state}))
    assert error.value.code == "tailscale_unavailable"


def test_client_uses_a_bounded_status_command_and_preserves_unicode_names(monkeypatch):
    monkeypatch.setenv("DROIDOCK_TAILSCALE_PATH", "C:/Custom Folder/tailscale.exe")
    response = subprocess.CompletedProcess([], 0, status(HostName="Device \u2605"), "")
    with patch("droidock.tailscale.subprocess.run", return_value=response) as run:
        client = TailscaleClient()
        run.assert_not_called()
        assert client.peers()[0].name == "Device \u2605"
    assert run.call_args is not None
    assert run.call_args.args[0] == ["C:/Custom Folder/tailscale.exe", "status", "--json"]
    assert run.call_args.kwargs["timeout"] == 5
    assert not run.call_args.kwargs.get("shell", False)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (FileNotFoundError(), "tailscale_missing"),
        (subprocess.TimeoutExpired("tailscale", 5), "tailscale_timeout"),
    ],
)
def test_client_reports_process_failures(failure, code):
    with patch("droidock.tailscale.subprocess.run", side_effect=failure):
        with pytest.raises(DroidockError) as error:
            TailscaleClient("tailscale").peers()
    assert error.value.code == code


def test_client_reports_logged_out_command_failure():
    with patch(
        "droidock.tailscale.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "log in")
    ):
        with pytest.raises(DroidockError) as error:
            TailscaleClient("tailscale").peers()
    assert error.value.code == "tailscale_unavailable"


def test_missing_optional_tailscale_does_not_start_a_process(monkeypatch):
    monkeypatch.delenv("DROIDOCK_TAILSCALE_PATH", raising=False)
    with (
        patch("droidock.tailscale.shutil.which", return_value=None),
        patch("pathlib.Path.is_file", return_value=False),
    ):
        with patch("droidock.tailscale.subprocess.run") as run:
            with pytest.raises(DroidockError) as error:
                TailscaleClient().peers()
            run.assert_not_called()
    assert error.value.code == "tailscale_missing"
