from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from droidock import AdbBackend, CommandError, DroidockError


@pytest.fixture
def backend(monkeypatch):
    result = AdbBackend()
    result._executable = Path("adb.exe")
    monkeypatch.setattr(result, "_check_server", lambda: None)
    return result


def test_command_api_preserves_output_exit_code_cwd_and_long_timeout(backend, monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 17, "partial output\n", "operation failed\n")

    monkeypatch.setattr(subprocess, "run", run)
    for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT"):
        monkeypatch.setenv(key, "remote-override")
    backend.settings.server_port = 5040
    result = backend.run(
        ["install", "app with spaces.apk"], serial="USB-A", timeout=180, cwd=tmp_path, check=False
    )
    assert result.returncode == 17
    assert result.stdout == "partial output\n"
    assert result.stderr == "operation failed\n"
    command, options = calls[0]
    assert command == ["adb.exe", "-P", "5040", "-s", "USB-A", "install", "app with spaces.apk"]
    assert options["timeout"] == 180 and options["cwd"] == tmp_path
    assert "ADB_SERVER_SOCKET" not in options["env"]
    assert os.environ["ADB_SERVER_SOCKET"] == "remote-override"
    assert not options.get("shell")


def test_command_exception_contains_redacted_structured_result(backend, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "654321", "654321")
    )
    with pytest.raises(CommandError) as error:
        backend.run(["pair", "10.0.0.1:40001"], input_text="654321\n")
    assert error.value.result.returncode == 1
    assert error.value.result.stdout == "[REDACTED]"
    assert "654321" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeout_is_rejected_before_adb_execution(backend, monkeypatch, timeout):
    monkeypatch.setattr(backend, "command", lambda *a, **k: pytest.fail("Unexpected ADB execution"))
    with pytest.raises(DroidockError) as error:
        backend.run(["devices"], timeout=timeout)
    assert error.value.code == "invalid_setting"


def test_argument_string_is_not_interpreted_as_shell_command(backend):
    with pytest.raises(DroidockError):
        backend.run("devices && echo wrong")


def test_command_timeout_has_stable_error_code(backend, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DroidockError) as error:
        backend.run(["shell", "sleep", "10"], timeout=0.1)
    assert error.value.code == "timeout"
