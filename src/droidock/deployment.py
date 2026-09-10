"""Verified single-APK installation and application control on one selected device."""

from __future__ import annotations

import hashlib
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import DroidockError
from .manager import ConnectionManager
from .models import CommandResult, Transport


@dataclass(frozen=True)
class InstallResult:
    package: str
    address: str
    sha256: str
    installed: bool
    method: str
    elapsed_seconds: float
    warnings: tuple[str, ...] = ()


def _package_name(package: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
        raise DroidockError("Specify a valid Android application ID.", code="invalid_package")
    return package


class Deployment:
    """Reuse a connection manager and pin all operations to its selected transport.

    No Unity/project discovery, automatic uninstall, or device switching occurs.
    A supplied Transport must already have a verified identity. After a connection
    failure the caller can reconnect explicitly and create a new deployment.
    """

    def __init__(self, manager: ConnectionManager, device: str | Transport | None = None) -> None:
        self.manager = manager
        self.device = device if isinstance(device, Transport) else manager.ensure_connected(device)
        if not self.device.ready:
            raise DroidockError("Deployment requires a verified device connection.", code="unverified_device")

    def _run(self, arguments: list[str], *, timeout: float = 30, check: bool = True) -> CommandResult:
        return self.manager.run(arguments, device=self.device, timeout=timeout, check=check)

    def _shell(self, *arguments: str, timeout: float = 30, check: bool = True) -> CommandResult:
        # adb shell joins its arguments on the remote shell; quote paths there too.
        return self._run(["shell", shlex.join(arguments)], timeout=timeout, check=check)

    def _remote_hash(self, path: str) -> str:
        result = self._shell("sha256sum", path, check=False)
        match = re.match(r"^([0-9a-fA-F]{64})\s", result.stdout.strip())
        return match.group(1).lower() if result.returncode == 0 and match else ""

    def _installed_matches(self, package: str, digest: str) -> bool:
        result = self._shell("pm", "path", package, check=False)
        paths = [line[8:].strip() for line in result.stdout.splitlines() if line.startswith("package:")]
        # A base APK plus installed splits is not equivalent to a standalone APK.
        return result.returncode == 0 and len(paths) == 1 and self._remote_hash(paths[0]) == digest

    def _push(self, apk: Path, remote: str, digest: str, timeout: float) -> None:
        try:
            result = self._run(["push", "-Z", str(apk), remote], timeout=timeout, check=False)
        except DroidockError as exc:
            if exc.code != "timeout":
                raise
            # ADB can lose the acknowledgement after the complete transfer.
            if self._remote_hash(remote) != digest:
                raise
        else:
            if result.returncode and self._remote_hash(remote) != digest:
                raise DroidockError(f"APK transfer failed: {result.output}", code="transfer_failed")
        size = self._shell("stat", "-c", "%s", remote, check=False)
        if size.returncode or size.stdout.strip() != str(apk.stat().st_size):
            raise DroidockError(
                "Uploaded APK size does not match the local file.", code="verification_failed"
            )
        if self._remote_hash(remote) != digest:
            raise DroidockError(
                "Uploaded APK hash does not match the local file.", code="verification_failed"
            )

    def install_apk(
        self,
        apk: str | Path,
        *,
        package: str,
        allow_downgrade: bool = False,
        strategy: Literal["auto", "streaming", "staged"] = "auto",
        timeout: float = 1800,
    ) -> InstallResult:
        """Install one standalone APK, replacing an existing app while preserving data.

        package is the APK's application ID, supplied by the caller. A readable
        installed APK and device sha256sum/stat commands are required for verification.
        timeout bounds each transfer/install command, not the entire operation.
        Auto uses streaming on USB and verified staging on wireless transports.
        Signature/version rejection never causes an uninstall or a second attempt.
        """
        import math

        _package_name(package)
        if strategy not in {"auto", "streaming", "staged"}:
            raise DroidockError("Unknown APK installation strategy.", code="invalid_setting")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise DroidockError("Timeout must be finite and positive.", code="invalid_setting")
        path = Path(apk).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".apk":
            raise DroidockError(f"APK file not found: {path}", code="invalid_apk")
        started = time.monotonic()
        try:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise DroidockError(f"Cannot read APK: {path}", code="invalid_apk") from exc
        warnings: list[str] = []

        def completed(installed: bool, method: str) -> InstallResult:
            return InstallResult(
                package,
                self.device.address,
                digest,
                installed,
                method,
                time.monotonic() - started,
                tuple(warnings),
            )

        if self._installed_matches(package, digest):
            return completed(False, "identical")
        options = ["-r", *(["-d"] if allow_downgrade else [])]
        if strategy == "streaming" or (strategy == "auto" and not self.device.wireless):
            try:
                result = self._run(
                    ["install", "--streaming", *options, str(path)], timeout=timeout, check=False
                )
            except DroidockError as exc:
                if exc.code == "timeout" and self._installed_matches(package, digest):
                    return completed(True, "streaming")
                raise
            if "Success" in result.output.splitlines() and result.returncode == 0:
                if not self._installed_matches(package, digest):
                    raise DroidockError(
                        "Installed APK hash does not match the local file.", code="verification_failed"
                    )
                return completed(True, "streaming")
            # Do not retry a package-manager rejection (signature, downgrade, storage, etc.).
            if (
                re.search(r"INSTALL_(?:FAILED|PARSE_FAILED)|\bFailure\s*\[", result.output)
                or strategy == "streaming"
            ):
                raise DroidockError(f"APK installation failed: {result.output}", code="install_failed")
            if self._installed_matches(package, digest):
                return completed(True, "streaming")

        remote = f"/data/local/tmp/droidock-{uuid.uuid4().hex}.apk"
        try:
            self._push(path, remote, digest, timeout)
            try:
                result = self._shell("pm", "install", *options, remote, timeout=timeout, check=False)
            except DroidockError as exc:
                if exc.code != "timeout" or not self._installed_matches(package, digest):
                    raise
            else:
                if result.returncode or "Success" not in result.output.splitlines():
                    raise DroidockError(f"APK installation failed: {result.output}", code="install_failed")
                if not self._installed_matches(package, digest):
                    raise DroidockError(
                        "Installed APK hash does not match the local file.", code="verification_failed"
                    )
        finally:
            try:
                result = self._shell("rm", "-f", "--", remote, check=False)
                if result.returncode:
                    warnings.append(f"Could not remove staged APK: {remote}")
            except DroidockError:
                # Cleanup must not replace the installation result or original failure.
                warnings.append(f"Could not remove staged APK: {remote}")
        return completed(True, "staged")

    def launch(self, package: str, *, activity: str | None = None) -> CommandResult:
        """Launch the application's launcher activity, or an explicitly named activity."""
        _package_name(package)
        if activity is None:
            resolved = self._shell(
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
            )
            activity = next(
                (line.strip() for line in reversed(resolved.stdout.splitlines()) if "/" in line), ""
            )
        elif "/" not in activity:
            activity = f"{package}/{activity}"
        if not activity.startswith(f"{package}/") or not re.fullmatch(r"[A-Za-z0-9_.$/]+", activity):
            raise DroidockError(
                "No matching launcher activity. Specify an activity explicitly.", code="activity_not_found"
            )
        result = self._shell(
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-n",
            activity,
        )
        if re.search(r"(?im)^\s*(?:Error|Exception|Status:\s*(?!ok\b)\S+)", result.output):
            raise DroidockError(f"Application launch failed: {result.output}", code="launch_failed")
        return result

    def stop(self, package: str) -> CommandResult:
        """Stop an application without clearing its data."""
        return self._shell("am", "force-stop", _package_name(package))
