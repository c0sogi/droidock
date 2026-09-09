"""Optional Tailscale peer enumeration. This module does not search TCP ports."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import DroidockError


@dataclass(frozen=True)
class TailscalePeer:
    id: str
    name: str
    dns_name: str
    os: str
    addresses: tuple[str, ...]
    online: bool
    peer_api_ports: tuple[int, ...] = ()


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def parse_tailscale_status(text: str) -> list[TailscalePeer]:
    """Read machine-readable status, ignoring unknown fields and unusable peer entries."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DroidockError("Tailscale returned invalid JSON.", code="tailscale_status_invalid") from exc
    if not isinstance(data, dict):
        raise DroidockError("Tailscale returned an invalid status.", code="tailscale_status_invalid")
    if data.get("BackendState") != "Running":
        raise DroidockError(
            "Start Tailscale and sign in before listing devices.", code="tailscale_unavailable"
        )
    raw_peers = data.get("Peer")
    if raw_peers is None:
        raw_peers = {}
    if not isinstance(raw_peers, dict):
        raise DroidockError("Tailscale returned an invalid peer list.", code="tailscale_status_invalid")
    peers: dict[str, TailscalePeer] = {}
    for key, item in raw_peers.items():
        if not isinstance(item, dict):
            continue
        raw_addresses = item.get("TailscaleIPs")
        if not isinstance(raw_addresses, list):
            continue
        addresses: list[str] = []
        for value in raw_addresses:
            if not isinstance(value, str):
                continue
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not address.is_unspecified and not address.is_multicast:
                addresses.append(str(address))
        if not addresses:
            continue

        addresses = sorted(set(addresses), key=lambda a: (ipaddress.ip_address(a).version, a))
        ports: set[int] = set()
        urls = item.get("PeerAPIURL", [])
        if isinstance(urls, list):
            for url in urls:
                if not isinstance(url, str):
                    continue
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.hostname
                        and str(ipaddress.ip_address(parsed.hostname)) in addresses
                        and parsed.port
                    ):
                        ports.add(parsed.port)
                except ValueError:
                    continue
        identifier = _text(item.get("ID"), key)
        dns_name = _text(item.get("DNSName")).rstrip(".")
        peers[identifier] = TailscalePeer(
            identifier,
            _text(item.get("HostName"), dns_name or addresses[0]),
            dns_name,
            _text(item.get("OS"), "unknown"),
            tuple(addresses),
            item.get("Online") is True,
            tuple(sorted(ports)),
        )
    return sorted(
        peers.values(), key=lambda p: (p.os.casefold() != "android", not p.online, p.name.casefold())
    )


class TailscaleClient:
    """Use the installed Tailscale CLI; construction/import does not start a subprocess."""

    def __init__(self, executable: str | Path | None = None) -> None:
        self.executable = executable

    def peers(self) -> list[TailscalePeer]:
        executable = str(self.executable or os.environ.get("DROIDOCK_TAILSCALE_PATH", ""))
        if not executable:
            executable = shutil.which("tailscale") or ""
        if not executable and os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe"
            if candidate.is_file():
                executable = str(candidate)
        if not executable:
            raise DroidockError(
                "Install and sign in to Tailscale to use this menu. If already installed, set DROIDOCK_TAILSCALE_PATH.",
                code="tailscale_missing",
            )
        try:
            result = subprocess.run(
                [executable, "status", "--json"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise DroidockError(
                "Tailscale did not respond within 5 seconds.", code="tailscale_timeout"
            ) from exc
        except OSError as exc:
            raise DroidockError(
                "Cannot run Tailscale. Check its installation or executable path.", code="tailscale_missing"
            ) from exc
        if result.returncode:
            raise DroidockError(
                "Cannot read Tailscale devices. Check that Tailscale is running and signed in.",
                code="tailscale_unavailable",
            )
        return parse_tailscale_status(result.stdout)
