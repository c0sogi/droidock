from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

from droidock import SCRCPY_RELEASE, DroidockError, ScrcpyAsset, ScrcpyInstaller, ScrcpyRelease
from droidock.scrcpy import parse_options, parse_version, platform_tag


def zip_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def tar_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def release_for(platform_name: str, name: str, data: bytes, *, digest: str | None = None) -> ScrcpyRelease:
    asset = ScrcpyAsset(platform_name, name, digest or hashlib.sha256(data).hexdigest(), len(data))
    return ScrcpyRelease("5.0.1", (asset,))


class Opener:
    def __init__(self, data: bytes = b"", failures: int = 0):
        self.data = data
        self.failures = failures
        self.urls: list[str] = []

    def __call__(self, request, *, timeout):
        self.urls.append(request.full_url)
        assert request.get_header("User-agent").startswith("droidock/")
        if self.failures:
            self.failures -= 1
            raise urllib.error.URLError("network unreachable")
        return io.BytesIO(self.data)


def reporting(version: str = "5.0.1", help_text: str = "  --max-size=value\n  --no-audio\n"):
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "--version":
            output = f"scrcpy {version} <https://github.com/Genymobile/scrcpy>\n\nDependencies:\n"
        else:
            output = help_text
        return subprocess.CompletedProcess(command, 0, output, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


WINDOWS_ARCHIVE = zip_archive(
    {
        "scrcpy-win64-v5.0.1/scrcpy.exe": b"binary",
        "scrcpy-win64-v5.0.1/scrcpy-server": b"server",
        "scrcpy-win64-v5.0.1/adb.exe": b"adb",
    }
)


def test_every_platform_tag_has_a_published_archive():
    expected = {
        ("Windows", "AMD64"): "win64",
        ("Windows", "ARM64"): "winarm64",
        ("Windows", "x86"): "win32",
        ("Linux", "x86_64"): "linux-x86_64",
        ("Darwin", "arm64"): "macos-aarch64",
        ("Darwin", "x86_64"): "macos-x86_64",
    }
    for (system, machine), tag in expected.items():
        assert platform_tag(system, machine) == tag
        asset = SCRCPY_RELEASE.asset(tag)
        assert asset is not None and len(asset.sha256) == 64 and asset.size > 1_000_000
        assert SCRCPY_RELEASE.url(asset).startswith(
            f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_RELEASE.version}/"
        )
    assert platform_tag("Linux", "aarch64") is None
    assert {asset.platform for asset in SCRCPY_RELEASE.assets} == set(expected.values())


def test_version_and_option_parsing():
    assert parse_version("scrcpy 5.0.1 <https://github.com/Genymobile/scrcpy>") == "5.0.1"
    assert parse_version("scrcpy v3.3") == "3.3"
    assert parse_version("adb 1.0.41") is None
    help_text = "    -m, --max-size=value\n    --no-audio\n        Disable --video-buffer.\n"
    assert parse_options(help_text) == frozenset({"max-size", "no-audio", "video-buffer"})


def test_download_is_verified_extracted_and_reused(tmp_path):
    opener = Opener(WINDOWS_ARCHIVE)
    runner = reporting()
    progress: list[tuple[int, int]] = []
    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("win64", "scrcpy-win64-v5.0.1.zip", WINDOWS_ARCHIVE),
        platform_name="win64",
        opener=opener,
        runner=runner,
    )
    install = installer.ensure(lambda received, total: progress.append((received, total)))
    assert install.source == "downloaded"
    assert install.version == "5.0.1"
    assert install.executable.name == "scrcpy.exe"
    assert install.executable.is_relative_to(tmp_path / "cache" / "5.0.1-win64")
    assert install.supports("--anything")  # the pinned release supports every option used
    assert progress[-1] == (len(WINDOWS_ARCHIVE), len(WINDOWS_ARCHIVE))
    assert opener.urls == [
        "https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip"
    ]
    assert not list((tmp_path / "cache").glob(".download-*"))
    assert not list((tmp_path / "cache").glob(".extract-*"))

    again = installer.ensure()
    assert again.source == "managed"
    assert again.executable == install.executable
    assert len(opener.urls) == 1


def test_digest_mismatch_deletes_the_download(tmp_path):
    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("win64", "scrcpy-win64-v5.0.1.zip", WINDOWS_ARCHIVE, digest="0" * 64),
        platform_name="win64",
        opener=Opener(WINDOWS_ARCHIVE),
        runner=reporting(),
    )
    with pytest.raises(DroidockError) as raised:
        installer.ensure()
    assert raised.value.code == "scrcpy_checksum"
    leftovers = [path.name for path in (tmp_path / "cache").iterdir()]
    assert leftovers in ([], [".install.lock"])


def test_archive_paths_outside_the_folder_are_rejected(tmp_path):
    archive = zip_archive({"scrcpy-win64-v5.0.1/scrcpy.exe": b"binary", "../escaped.exe": b"evil"})
    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("win64", "scrcpy-win64-v5.0.1.zip", archive),
        platform_name="win64",
        opener=Opener(archive),
        runner=reporting(),
    )
    with pytest.raises(DroidockError) as raised:
        installer.ensure()
    assert raised.value.code == "scrcpy_install_failed"
    assert not (tmp_path / "escaped.exe").exists()
    assert not (tmp_path / "cache" / "5.0.1-win64").exists()


def test_tar_archives_install_with_a_runnable_binary(tmp_path):
    archive = tar_archive(
        {
            "scrcpy-linux-x86_64-v5.0.1/scrcpy": b"binary",
            "scrcpy-linux-x86_64-v5.0.1/scrcpy-server": b"server",
        }
    )
    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("linux-x86_64", "scrcpy-linux-x86_64-v5.0.1.tar.gz", archive),
        platform_name="linux-x86_64",
        opener=Opener(archive),
        runner=reporting(),
    )
    install = installer.ensure()
    assert install.executable.name == "scrcpy"
    if os.name != "nt":
        assert install.executable.stat().st_mode & 0o100


def test_configured_executable_is_used_without_downloads(tmp_path):
    executable = tmp_path / "scrcpy.exe"
    with pytest.raises(DroidockError) as raised:
        ScrcpyInstaller(tmp_path / "cache", executable=executable).find()
    assert raised.value.code == "scrcpy_missing"

    executable.write_bytes(b"binary")
    installer = ScrcpyInstaller(
        tmp_path / "cache", executable=executable, runner=reporting("5.0", "--max-size --no-audio")
    )
    install = installer.ensure()
    assert install.source == "configured"
    assert install.supports("--max-size") and not install.supports("--no-terminal-title")

    old = ScrcpyInstaller(tmp_path / "cache", executable=executable, runner=reporting("2.4"))
    with pytest.raises(DroidockError) as raised:
        old.find()
    assert raised.value.code == "scrcpy_outdated"


def test_environment_selects_the_executable_and_cache(tmp_path, monkeypatch):
    executable = tmp_path / "custom-scrcpy"
    executable.write_bytes(b"binary")
    monkeypatch.setenv("DROIDOCK_SCRCPY_PATH", str(executable))
    monkeypatch.setenv("DROIDOCK_SCRCPY_HOME", str(tmp_path / "custom-cache"))
    installer = ScrcpyInstaller(runner=reporting())
    assert installer.directory == (tmp_path / "custom-cache").resolve()
    assert installer.ensure().executable == executable.resolve()


def test_scrcpy_on_path_is_used_only_when_recent(tmp_path):
    found = tmp_path / "bin" / "scrcpy"
    found.parent.mkdir()
    found.write_bytes(b"binary")
    recent = ScrcpyInstaller(
        tmp_path / "cache",
        platform_name="win64",
        which=lambda name: str(found),
        runner=reporting("3.1", "--max-size\n--print-fps\n"),
    )
    install = recent.find()
    assert install is not None and install.source == "path" and install.version == "3.1"
    assert install.options == frozenset({"max-size", "print-fps"})

    outdated = ScrcpyInstaller(
        tmp_path / "cache", platform_name="win64", which=lambda name: str(found), runner=reporting("2.7")
    )
    assert outdated.find() is None


def test_unsupported_platforms_explain_a_manual_install(tmp_path):
    installer = ScrcpyInstaller(tmp_path / "cache", platform_name=None)
    assert installer.install_directory is None
    with pytest.raises(DroidockError) as raised:
        installer.ensure()
    assert raised.value.code == "scrcpy_unavailable"
    assert "DROIDOCK_SCRCPY_PATH" in str(raised.value)


def test_download_failures_retry_once_then_explain(tmp_path):
    opener = Opener(WINDOWS_ARCHIVE, failures=2)
    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("win64", "scrcpy-win64-v5.0.1.zip", WINDOWS_ARCHIVE),
        platform_name="win64",
        opener=opener,
        runner=reporting(),
    )
    with pytest.raises(DroidockError) as raised:
        installer.ensure()
    assert raised.value.code == "scrcpy_download_failed"
    assert "network unreachable" in str(raised.value)
    assert len(opener.urls) == 2

    opener.failures = 1
    assert installer.ensure().source == "downloaded"


def test_a_broken_managed_copy_is_reinstalled(tmp_path):
    broken = tmp_path / "cache" / "5.0.1-win64" / "scrcpy-win64-v5.0.1"
    broken.mkdir(parents=True)
    (broken / "scrcpy.exe").write_bytes(b"truncated")

    def flaky_runner(command, **kwargs):
        if Path(command[0]).read_bytes() == b"truncated":
            raise OSError("not a valid application")
        return reporting()(command, **kwargs)

    installer = ScrcpyInstaller(
        tmp_path / "cache",
        release=release_for("win64", "scrcpy-win64-v5.0.1.zip", WINDOWS_ARCHIVE),
        platform_name="win64",
        opener=Opener(WINDOWS_ARCHIVE),
        runner=flaky_runner,
    )
    install = installer.ensure()
    assert install.source == "downloaded"
    assert Path(install.executable).read_bytes() == b"binary"
