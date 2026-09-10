from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import FakeBackend, FakeDiscovery, connected
from typer.testing import CliRunner

from droidock import (
    AdbBackend,
    CommandResult,
    ConnectionManager,
    Deployment,
    DeviceStore,
    DroidockError,
    Settings,
    Transport,
)
from droidock.cli import app


class PackageBackend(FakeBackend):
    """Emulate package and file state, including lost acknowledgements after mutation."""

    def __init__(self, address: str):
        super().__init__()
        self.online[address] = connected(address)
        self.calls: list[tuple[str | None, list[str]]] = []
        self.files: dict[str, bytes] = {}
        self.installed: bytes | None = None
        self.streaming_error = ""
        self.push_timeout = False
        self.install_timeout = False
        self.cleanup_failure = False
        self.corrupt_transfer = False
        self.wrong_installed_hash = False
        self.launch_error = False
        self.split = False

    def run(self, arguments, *, serial=None, **kwargs):
        args = shlex.split(arguments[1]) if arguments[0] == "shell" else list(arguments)
        self.calls.append((serial, args))
        assert serial in self.online, "must retain the selected transport"
        if args[:2] == ["pm", "path"]:
            paths = "package:/data/app/fixture/base.apk\n" if self.installed else ""
            if self.split:
                paths += "package:/data/app/fixture/split.apk\n"
            return CommandResult(0, paths, "")
        if args[0] == "sha256sum":
            data = self.installed if args[1].endswith("/base.apk") else self.files.get(args[1])
            digest = hashlib.sha256(data).hexdigest() if data else ""
            return CommandResult(0 if data else 1, f"{digest}  {args[1]}\n", "")
        if args[0] == "stat":
            return CommandResult(0, str(len(self.files.get(args[-1], b""))), "")
        if args[0] == "push":
            self.files[args[-1]] = b"corrupted" if self.corrupt_transfer else Path(args[-2]).read_bytes()
            if self.push_timeout:
                raise DroidockError("Lost transfer acknowledgement", code="timeout")
            return CommandResult(0, "transferred", "")
        if args[0] == "install" or args[:2] == ["pm", "install"]:
            if args[0] == "install" and self.streaming_error:
                return CommandResult(1, "", self.streaming_error)
            self.installed = Path(args[-1]).read_bytes() if args[0] == "install" else self.files[args[-1]]
            self.split = False
            if self.wrong_installed_hash:
                self.installed = b"another apk"
            if self.install_timeout:
                raise DroidockError("Lost install acknowledgement", code="timeout")
            return CommandResult(0, "Success\n", "")
        if args[0] == "rm":
            if self.cleanup_failure:
                raise DroidockError("cleanup unavailable", code="timeout")
            self.files.pop(args[-1], None)
            return CommandResult(0, "", "")
        if args[:3] == ["cmd", "package", "resolve-activity"]:
            return CommandResult(0, "com.example.app/.MainActivity\n", "")
        if args[:2] == ["am", "start"]:
            return CommandResult(0, "Error: activity missing" if self.launch_error else "Status: ok", "")
        if args[:2] == ["am", "force-stop"]:
            return CommandResult(0, "", "")
        raise AssertionError(args)


def setup(tmp_path, address="USB-A"):
    backend = PackageBackend(address)
    manager = ConnectionManager(DeviceStore(tmp_path / "state"), backend=backend, discovery=FakeDiscovery())
    apk = tmp_path / "(테스트) 한글 app's.apk"
    apk.write_bytes(b"standalone-apk")
    return Deployment(manager, backend.online[address]), backend, apk


@pytest.mark.parametrize("address,method", [("USB-A", "streaming"), ("192.0.2.1:40001", "staged")])
def test_install_verify_and_skip_identical_without_reselecting_device(tmp_path, address, method):
    deployment, backend, apk = setup(tmp_path, address)
    with patch.object(
        deployment.manager, "ensure_connected", side_effect=AssertionError("must not reselect")
    ):
        result = deployment.install_apk(apk, package="com.example.app")
        assert result.installed and result.method == method
        assert result.address == address
        before = len(backend.calls)
        result = deployment.install_apk(apk, package="com.example.app")
    assert not result.installed
    assert all(args[0] not in {"install", "push"} for _, args in backend.calls[before:])
    assert not backend.files
    assert all(serial == address for serial, _ in backend.calls)
    assert all("-d" not in args for _, args in backend.calls)


def test_auto_falls_back_to_verified_staging_for_unsupported_streaming(tmp_path):
    deployment, backend, apk = setup(tmp_path)
    backend.streaming_error = "unknown option --streaming"
    result = deployment.install_apk(apk, package="com.example.app", allow_downgrade=True)
    assert result.method == "staged"
    assert backend.installed == apk.read_bytes()
    assert any(args[:4] == ["pm", "install", "-r", "-d"] for _, args in backend.calls)
    assert not backend.files


@pytest.mark.parametrize(
    "failure",
    [
        "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
        "INSTALL_FAILED_VERSION_DOWNGRADE",
        "INSTALL_PARSE_FAILED_MANIFEST_MALFORMED",
        "Failure [PACKAGE_REJECTED]",
    ],
)
def test_package_rejection_never_retries_or_uninstalls(tmp_path, failure):
    deployment, backend, apk = setup(tmp_path)
    backend.streaming_error = failure
    with pytest.raises(DroidockError) as error:
        deployment.install_apk(apk, package="com.example.app")
    assert failure in str(error.value)
    assert not any(args[0] in {"push", "uninstall"} for _, args in backend.calls)
    assert sum(args[0] == "install" for _, args in backend.calls) == 1


@pytest.mark.parametrize("address", ["USB-A", "192.0.2.1:40001"])
def test_lost_install_acknowledgement_is_verified_before_returning(tmp_path, address):
    deployment, backend, apk = setup(tmp_path, address)
    backend.install_timeout = True
    assert deployment.install_apk(apk, package="com.example.app").installed
    assert not backend.files


def test_completed_upload_can_recover_lost_acknowledgement(tmp_path):
    deployment, backend, apk = setup(tmp_path, "192.0.2.1:40001")
    backend.push_timeout = True
    assert deployment.install_apk(apk, package="com.example.app").installed


@pytest.mark.parametrize("timeout", [False, True])
def test_corrupt_upload_cannot_reach_package_manager(tmp_path, timeout):
    deployment, backend, apk = setup(tmp_path, "192.0.2.1:40001")
    backend.corrupt_transfer = True
    backend.push_timeout = timeout
    with pytest.raises(DroidockError):
        deployment.install_apk(apk, package="com.example.app")
    assert backend.installed is None
    assert not backend.files


@pytest.mark.parametrize("address", ["USB-A", "192.0.2.1:40001"])
def test_install_success_requires_matching_installed_hash(tmp_path, address):
    deployment, backend, apk = setup(tmp_path, address)
    backend.wrong_installed_hash = True
    with pytest.raises(DroidockError) as error:
        deployment.install_apk(apk, package="com.example.app")
    assert error.value.code == "verification_failed"


def test_cleanup_error_does_not_replace_failure_or_success(tmp_path):
    deployment, backend, apk = setup(tmp_path, "192.0.2.1:40001")
    backend.cleanup_failure = True
    backend.wrong_installed_hash = True
    with pytest.raises(DroidockError) as error:
        deployment.install_apk(apk, package="com.example.app")
    assert error.value.code == "verification_failed"
    backend.wrong_installed_hash = False
    result = deployment.install_apk(apk, package="com.example.app")
    assert result.installed and result.warnings


def test_existing_splits_are_not_reported_as_identical(tmp_path):
    deployment, backend, apk = setup(tmp_path)
    backend.installed = apk.read_bytes()
    backend.split = True
    assert deployment.install_apk(apk, package="com.example.app").installed


def test_launch_and_stop_use_selected_device_and_detect_shell_level_failure(tmp_path):
    deployment, backend, _ = setup(tmp_path)
    deployment.launch("com.example.app")
    deployment.stop("com.example.app")
    assert backend.calls[-1] == ("USB-A", ["am", "force-stop", "com.example.app"])
    backend.launch_error = True
    with pytest.raises(DroidockError) as error:
        deployment.launch("com.example.app")
    assert error.value.code == "launch_failed"


@pytest.mark.parametrize("package", ["com.example;reboot", "com.example app", ""])
def test_invalid_package_never_executes_a_command(tmp_path, package):
    deployment, backend, apk = setup(tmp_path)
    with pytest.raises(DroidockError):
        deployment.install_apk(apk, package=package)
    assert not backend.calls


def test_unverified_transport_is_rejected(tmp_path):
    deployment, _, _ = setup(tmp_path)
    with pytest.raises(DroidockError):
        Deployment(deployment.manager, Transport("USB-B", "unauthorized"))


def test_unreadable_apk_is_a_structured_error_before_device_commands(tmp_path):
    deployment, backend, apk = setup(tmp_path)
    with patch.object(Path, "open", side_effect=PermissionError("not readable")):
        with pytest.raises(DroidockError) as error:
            deployment.install_apk(apk, package="com.example.app")
    assert error.value.code == "invalid_apk"
    assert not backend.calls


def test_executable_preview_does_not_run_adb_but_execution_still_validates_it(tmp_path):
    executable = tmp_path / "adb"
    executable.touch()
    backend = AdbBackend(Settings(adb_path=str(executable)))
    with patch("droidock.adb.subprocess.run", side_effect=OSError("not executable")) as run:
        assert backend.executable_path == executable.resolve()
        run.assert_not_called()
        with pytest.raises(DroidockError, match="Cannot run"):
            _ = backend.executable


def test_cli_install_json_and_launch_from_an_unrelated_directory(tmp_path, monkeypatch):
    deployment, backend, apk = setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    with patch("droidock.cli.ConnectionManager", return_value=deployment.manager):
        result = CliRunner().invoke(
            app,
            ["install", str(apk), "--package", "com.example.app", "--device", "USB-A", "--launch", "--json"],
        )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["installed"] and document["launched"]
    assert any(args[:2] == ["am", "start"] for _, args in backend.calls)


def test_interactive_install_uses_saved_device_and_can_launch(tmp_path):
    from io import StringIO

    from rich.console import Console

    from droidock.interactive import InteractiveCli

    deployment, backend, apk = setup(tmp_path)
    menu = InteractiveCli(deployment.manager, console=Console(file=StringIO()))
    with (
        patch.object(menu, "text", side_effect=[str(apk), "com.example.app"]),
        patch.object(menu, "confirm", return_value=True),
        patch.object(menu, "_pause"),
    ):
        menu.install_apk("USB-A")
    assert backend.installed == apk.read_bytes()
    assert any(args[:2] == ["am", "start"] for _, args in backend.calls)
