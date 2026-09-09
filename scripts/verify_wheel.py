"""Run from outside the repository with only the built wheel installed.

--cold-start also starts an ADB server on a fresh local port and stops only that server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from importlib.metadata import distribution
from pathlib import Path

import droidock
from droidock import (
    AdbPortScanner,
    DeviceStore,
    PortScanStatus,
    Service,
    ServiceKind,
    Settings,
    TailscaleClient,
)
from droidock.adb import AdbBackend


def verify_cold_start(directory: Path) -> dict[str, object]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port != 5037
    os.environ["ADB_MDNS_AUTO_CONNECT"] = ""
    os.environ["ANDROID_USER_HOME"] = str(directory / "adb-user")
    backend = AdbBackend(Settings(server_port=port, command_timeout=15))
    executable = backend.executable
    diagnostics: dict[str, str] = {}
    print(f"Checking temporary ADB server on port {port}", file=sys.stderr, flush=True)
    try:
        transports = backend.transports()
        diagnostics = backend.diagnostics()
        return {"port": port, "transport_count": len(transports), "diagnostics": diagnostics}
    finally:
        diagnostics = diagnostics or backend.diagnostics()
        status = diagnostics.get("server", "").replace("\\\\", "\\")
        assert str(executable).casefold() in status.casefold(), diagnostics
        subprocess.run(
            [str(executable), "-H", "127.0.0.1", "-P", str(port), "kill-server"],
            check=True,
            timeout=15,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-start", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    package = Path(droidock.__file__).resolve()
    assert project not in package.parents, f"Install the wheel into an isolated environment: {package}"
    assert "typer" not in sys.modules and "questionary" not in sys.modules and "rich" not in sys.modules
    metadata = distribution("droidock")
    assert metadata.metadata["Name"] == "droidock"
    service = Service("wheel-check", ServiceKind.CONNECT, "127.0.0.1:40001")
    payload = json.loads(json.dumps(asdict(service)))
    assert payload["kind"] == "connect"
    assert Service(**payload).kind is ServiceKind.CONNECT
    assert TailscaleClient().executable is None
    stop = threading.Event()
    stop.set()
    cancelled = asyncio.run(AdbPortScanner().scan("127.0.0.1", ports=[40001], stop=stop))
    assert cancelled.status is PortScanStatus.CANCELLED and cancelled.completed == 0
    entry_points = {
        entry.name: entry.value for entry in metadata.entry_points if entry.group == "console_scripts"
    }
    assert entry_points == {"droidock": "droidock.cli:main"}, entry_points
    dependencies = metadata.requires or []
    assert not any("file:" in value or " @ " in value for value in dependencies)
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("DROIDOCK_ADB_PATH", None)
    os.environ["PATH"] = ""
    os.environ["PYTHONIOENCODING"] = "utf-8"

    with tempfile.TemporaryDirectory(prefix="droidock-wheel-check-") as folder:
        directory = Path(folder).resolve()
        assert directory.parent == Path(tempfile.gettempdir()).resolve()

        def run(*arguments: str) -> str:
            result = subprocess.run(
                [sys.executable, "-m", "droidock", "--data-dir", str(directory), *arguments],
                cwd=directory,
                capture_output=True,
                encoding="utf-8",
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        assert "Android" in run("--help")
        assert run("--version").strip() == f"droidock {droidock.__version__}"
        assert "Connection settings" in run("settings", "auto_connect_on_start", "false")
        assert not json.loads(run("settings", "--json"))["auto_connect_on_start"]
        assert not DeviceStore(directory).read().settings.auto_connect_on_start
        backend = AdbBackend()
        assert "adbutils" in backend.executable.parts
        result: dict[str, object] = {
            "package": str(package),
            "entry_points": entry_points,
            "version_ok": True,
            "dependencies": dependencies,
            "core_imports_no_cli": True,
            "service_kind_enum_json_ok": True,
            "tailscale_api_and_scan_cancellation_ok": True,
            "help_ok": True,
            "settings_persisted": True,
            "path_was_empty": True,
            "bundled_adb": str(backend.executable),
            "adb_version": backend._version,
        }
        if args.cold_start:
            result["cold_start"] = verify_cold_start(directory)
            result["temporary_server_stopped"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
