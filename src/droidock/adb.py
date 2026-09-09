from __future__ import annotations

import importlib.resources
import os
import re
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .errors import DroidockError
from .models import (
    ADB_SERVICE_KINDS,
    Identity,
    PairResult,
    Service,
    Settings,
    Transport,
    normalize_endpoint,
    valid_serial,
)


def parse_services(text: str) -> list[Service]:
    result: list[Service] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        instance, service_type, address = parts[0], parts[-2].rstrip("."), parts[-1]
        kind = ADB_SERVICE_KINDS.get(service_type)
        if kind is None:
            continue
        try:
            endpoint = normalize_endpoint(address)
        except DroidockError:
            continue
        result.append(Service(instance, kind, endpoint, "adb"))
    return result


class AdbBackend:
    """ADB adapter with bundled executable discovery and time-limited subprocesses.

    Existing compatible servers are reused. No operation kills the shared ADB server.
    Pairing secrets go through stdin and are removed from exception messages.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.validate()
        self._executable: Path | None = None
        self._version = ""
        self._protocol: int | None = None

    @property
    def executable(self) -> Path:
        if self._executable:
            return self._executable
        override = self.settings.adb_path or os.environ.get("DROIDOCK_ADB_PATH", "")
        if override:
            candidate = Path(override).expanduser()
            if not candidate.is_file():
                raise DroidockError(
                    f"The configured ADB executable does not exist: {candidate}", code="adb_missing"
                )
        else:
            bundled = importlib.resources.files("adbutils.binaries").joinpath(
                "adb.exe" if os.name == "nt" else "adb"
            )
            candidate = Path(str(bundled))
            if not candidate.is_file():
                found = shutil.which("adb")
                if not found:
                    raise DroidockError(
                        "No bundled ADB is available for this OS. Set an ADB executable path in settings.",
                        code="adb_missing",
                    )
                candidate = Path(found)
        try:
            result = subprocess.run(
                [str(candidate), "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.settings.command_timeout,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DroidockError(f"Cannot run the ADB executable: {candidate}", code="adb_missing") from exc
        match = re.search(r"Android Debug Bridge version 1\.0\.(\d+)", result.stdout)
        if not match:
            raise DroidockError("Cannot read the ADB version response.", code="adb_missing")
        self._protocol = int(match.group(1))
        self._version = result.stdout.strip()
        self._executable = candidate.resolve()
        return self._executable

    def _check_server(self) -> None:
        try:
            # Windows can take about a second to reject a closed localhost port.
            # A shorter timeout would mistake an absent server for an unresponsive one.
            connection = socket.create_connection(("127.0.0.1", self.settings.server_port), timeout=3)
        except ConnectionRefusedError:
            return
        except OSError as exc:
            raise DroidockError(f"Cannot reach the ADB server: {exc}", code="server_unavailable") from exc
        try:
            with connection:

                def receive(count: int) -> bytes:
                    data = b""
                    while len(data) < count:
                        chunk = connection.recv(count - len(data))
                        if not chunk:
                            raise ValueError("Incomplete server response")
                        data += chunk
                    return data

                request = b"host:version"
                connection.sendall(f"{len(request):04x}".encode() + request)
                if receive(4) != b"OKAY":
                    raise ValueError("The response is not from an ADB server")
                length = int(receive(4), 16)
                if length > 32:
                    raise ValueError("Invalid server version response")
                protocol = int(receive(length), 16)
                if protocol != self._protocol:
                    raise DroidockError(
                        "The running ADB server uses a different protocol version. Select a compatible ADB executable "
                        "or a separate server port. The shared server will not be stopped automatically.",
                        code="server_conflict",
                    )
        except (OSError, ValueError) as exc:
            raise DroidockError(
                f"The ADB server on local port {self.settings.server_port} is not responding correctly.",
                code="server_unavailable",
            ) from exc

    def _run(self, *arguments: str, input_text: str | None = None, timeout: float | None = None) -> str:
        executable = self.executable
        self._check_server()
        environment = os.environ.copy()
        # Explicitly select the checked local server, including when a parent app uses a remote server.
        environment.pop("ADB_SERVER_SOCKET", None)
        environment.pop("ANDROID_ADB_SERVER_ADDRESS", None)
        environment.pop("ANDROID_ADB_SERVER_PORT", None)
        # Even '-H 127.0.0.1' makes ADB refuse to start a missing server as a "remote host".
        # Removing remote-server overrides and using -P selects localhost and permits startup.
        command = [str(executable), "-P", str(self.settings.server_port), *arguments]
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.settings.command_timeout,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DroidockError("The device did not respond within the timeout.", code="timeout") from exc
        except OSError as exc:
            raise DroidockError(f"Failed to run ADB: {exc}", code="adb_failed") from exc
        output = "\n".join(x.strip() for x in (result.stdout, result.stderr) if x.strip())
        if input_text:
            output = output.replace(input_text.strip(), "[REDACTED]")
        if result.returncode:
            code = "unauthorized" if "unauthorized" in output.lower() else "adb_failed"
            raise DroidockError(output[-2500:] or "The ADB command failed.", code=code)
        return output

    def inspect(self, address: str) -> Transport:
        output = self._run("-s", address, "shell", "getprop")
        properties = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", output, flags=re.MULTILINE))
        serial = properties.get("ro.serialno", "")
        if not valid_serial(serial):
            serial = properties.get("ro.boot.serialno", "")
        if not valid_serial(serial):
            return Transport(address, "device", detail="Cannot determine the device serial number.")
        identity = Identity(
            serial,
            properties.get("ro.product.manufacturer", ""),
            properties.get("ro.product.model", ""),
            properties.get("persist.adb.wifi.guid", ""),
        )
        return Transport(address, "device", identity)

    def transports(self) -> list[Transport]:
        output = self._run("devices", "-l")
        entries: list[tuple[str, str]] = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] in {
                "device",
                "offline",
                "unauthorized",
                "recovery",
                "sideload",
            }:
                entries.append((fields[0], fields[1]))
            elif len(fields) >= 3 and fields[1:3] == ["no", "permissions"]:
                entries.append((fields[0], "no permissions"))

        def inspect_entry(entry: tuple[str, str]) -> Transport:
            address, state = entry
            if state != "device":
                return Transport(address, state)
            try:
                return self.inspect(address)
            except DroidockError as exc:
                return Transport(address, "unresponsive", detail=str(exc))

        with ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(inspect_entry, entries))

    def services(self) -> list[Service]:
        return parse_services(self._run("mdns", "services"))

    def connect(self, endpoint: str) -> None:
        endpoint = normalize_endpoint(endpoint)
        output = self._run("connect", endpoint)
        if not re.search(r"(?:already )?connected to ", output, re.IGNORECASE):
            raise DroidockError(output or "The connection could not be established.", code="connect_failed")

    def pair(self, endpoint: str, code: str) -> PairResult:
        endpoint = normalize_endpoint(endpoint)
        if not re.fullmatch(r"\d{6}", code):
            raise DroidockError("Enter the six-digit pairing code shown on the device.", code="invalid_code")
        output = self._run("pair", endpoint, input_text=code + "\n", timeout=30)
        if "successfully paired" not in output.lower():
            raise DroidockError(output or "Pairing failed.", code="pair_failed")
        match = re.search(r"\[guid=([^\]]+)\]", output)
        return PairResult(endpoint, match.group(1) if match else "")

    def disconnect(self, endpoint: str) -> None:
        if not re.fullmatch(r"adb-[A-Za-z0-9._-]+\._adb-tls-connect\._tcp\.?", endpoint):
            endpoint = normalize_endpoint(endpoint)
        self._run("disconnect", endpoint)

    def reconnect(self, address: str) -> None:
        self._run("-s", address, "reconnect")

    def diagnostics(self) -> dict[str, str]:
        result = {
            "executable": str(self.executable),
            "version": self._version,
            "server_port": str(self.settings.server_port),
        }
        for key, args in {"server": ("server-status",), "mdns": ("mdns", "check")}.items():
            try:
                result[key] = self._run(*args)
            except DroidockError as exc:
                result[key] = str(exc)
        return result
