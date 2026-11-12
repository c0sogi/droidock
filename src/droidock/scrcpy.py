"""Find scrcpy, or install a pinned release after checking its published SHA-256 digest.

Nothing is installed system-wide: a download is extracted into the per-user cache and used
from there. An explicit executable, an earlier managed copy, or a recent scrcpy on PATH is
used without any network access.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from platformdirs import user_cache_path

from .errors import DroidockError

MINIMUM_VERSION = (3, 0)
DOWNLOAD_ATTEMPTS = 2
DOWNLOAD_TIMEOUT = 30.0

ProgressCallback = Callable[[int, int], None]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class ScrcpyAsset:
    """One prebuilt archive of a release and its published SHA-256 digest."""

    platform: str
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ScrcpyRelease:
    version: str
    assets: tuple[ScrcpyAsset, ...]

    def asset(self, platform_name: str | None) -> ScrcpyAsset | None:
        return next((asset for asset in self.assets if asset.platform == platform_name), None)

    def url(self, asset: ScrcpyAsset) -> str:
        return f"https://github.com/Genymobile/scrcpy/releases/download/v{self.version}/{asset.name}"


# Digests published with the release (GitHub asset digests and doc/windows.md, linux.md, macos.md).
SCRCPY_RELEASE = ScrcpyRelease(
    "5.0.1",
    (
        ScrcpyAsset(
            "win64",
            "scrcpy-win64-v5.0.1.zip",
            "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5",
            11264634,
        ),
        ScrcpyAsset(
            "win32",
            "scrcpy-win32-v5.0.1.zip",
            "0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad",
            10133753,
        ),
        ScrcpyAsset(
            "winarm64",
            "scrcpy-winarm64-v5.0.1.zip",
            "16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b",
            9825739,
        ),
        ScrcpyAsset(
            "linux-x86_64",
            "scrcpy-linux-x86_64-v5.0.1.tar.gz",
            "9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e",
            17883742,
        ),
        ScrcpyAsset(
            "macos-aarch64",
            "scrcpy-macos-aarch64-v5.0.1.tar.gz",
            "33611e51977a8289e2e124b220f727895181ef2eb9060e0987a4dc2db9cf0d6b",
            12565912,
        ),
        ScrcpyAsset(
            "macos-x86_64",
            "scrcpy-macos-x86_64-v5.0.1.tar.gz",
            "31a5467a9e907f162b093267cbbab1276b15c6e709c2ea8159566a8d165beb50",
            13478118,
        ),
    ),
)


@dataclass(frozen=True)
class ScrcpyInstall:
    """A runnable scrcpy. An empty option set means every option of the pinned release."""

    executable: Path
    version: str
    source: str
    options: frozenset[str] = frozenset()

    def supports(self, option: str) -> bool:
        return not self.options or option.lstrip("-") in self.options


def find_on_path(name: str) -> str | None:
    return shutil.which(name)


def open_url(request: urllib.request.Request, *, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def platform_tag(system: str | None = None, machine: str | None = None) -> str | None:
    """The release archive suffix for this OS and CPU, or None when none is published."""
    system = (system if system is not None else platform.system()).casefold()
    machine = (machine if machine is not None else platform.machine()).casefold()
    if system == "windows":
        if machine in {"arm64", "aarch64"}:
            return "winarm64"
        if machine in {"amd64", "x86_64", "x64"}:
            return "win64"
        if machine in {"x86", "i386", "i686"}:
            return "win32"
    elif system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    elif system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "macos-aarch64"
        if machine in {"x86_64", "amd64"}:
            return "macos-x86_64"
    return None


def parse_version(text: str) -> str | None:
    match = re.search(r"\bscrcpy\s+v?(\d+(?:\.\d+){1,2})\b", text)
    return match.group(1) if match else None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def parse_options(text: str) -> frozenset[str]:
    """Long option names listed by ``scrcpy --help``."""
    return frozenset(re.findall(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)", text))


class ScrcpyInstaller:
    """Resolve the scrcpy used for mirroring.

    Order: an explicit executable (argument or DROIDOCK_SCRCPY_PATH), the managed copy of the
    pinned release, scrcpy on PATH if it is at least version 3.0, then a verified download of
    the pinned release into the user cache (DROIDOCK_SCRCPY_HOME overrides the directory).
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        executable: str | Path | None = None,
        release: ScrcpyRelease = SCRCPY_RELEASE,
        platform_name: str | None = "",
        opener: Callable[..., Any] | None = None,
        runner: Runner | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        configured = directory or os.environ.get("DROIDOCK_SCRCPY_HOME")
        self.directory = (
            Path(configured).expanduser().resolve()
            if configured
            else user_cache_path("droidock", appauthor=False) / "scrcpy"
        )
        self.executable = str(executable or os.environ.get("DROIDOCK_SCRCPY_PATH") or "")
        self.release = release
        self.platform = platform_tag() if platform_name == "" else platform_name
        self._opener = opener or open_url
        self._runner: Runner = runner or subprocess.run
        self._which = which or find_on_path

    @property
    def install_directory(self) -> Path | None:
        if self.release.asset(self.platform) is None:
            return None
        return self.directory / f"{self.release.version}-{self.platform}"

    def find(self) -> ScrcpyInstall | None:
        """Return a usable scrcpy without downloading anything."""
        if self.executable:
            path = Path(self.executable).expanduser()
            if not path.is_file():
                raise DroidockError(
                    f"The configured scrcpy executable does not exist: {path}", code="scrcpy_missing"
                )
            install = self._inspect(path, "configured", read_options=True)
            if version_tuple(install.version) < MINIMUM_VERSION:
                raise DroidockError(
                    f"scrcpy {install.version} is too old for mirroring; use version 3.0 or later.",
                    code="scrcpy_outdated",
                )
            return install
        managed = self._managed()
        if managed is not None:
            return managed
        found = self._which("scrcpy")
        if found:
            try:
                install = self._inspect(Path(found), "path", read_options=True)
            except DroidockError:
                return None
            if version_tuple(install.version) >= MINIMUM_VERSION:
                return install
        return None

    def ensure(self, progress: ProgressCallback | None = None) -> ScrcpyInstall:
        """Return a usable scrcpy, downloading the pinned release on first use."""
        return self.find() or self.install(progress)

    def install(self, progress: ProgressCallback | None = None) -> ScrcpyInstall:
        asset = self.release.asset(self.platform)
        destination = self.install_directory
        if asset is None or destination is None:
            raise DroidockError(
                "No prebuilt scrcpy is published for this computer. Install scrcpy 3.0 or later with "
                "the system package manager, or set DROIDOCK_SCRCPY_PATH.",
                code="scrcpy_unavailable",
            )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.directory / ".install.lock"), timeout=600):
                existing = self._managed()
                if existing is not None:
                    return existing
                archive = self._download(asset, progress)
                try:
                    self._extract(archive, asset, destination)
                finally:
                    archive.unlink(missing_ok=True)
        except Timeout as exc:
            raise DroidockError(
                "Another process is installing scrcpy. Try again shortly.", code="scrcpy_busy"
            ) from exc
        except OSError as exc:
            raise DroidockError(
                f"Cannot install scrcpy into {destination}: {exc}", code="scrcpy_install_failed"
            ) from exc
        installed = self._managed()
        if installed is None:
            raise DroidockError(
                "The scrcpy archive did not contain a runnable scrcpy.", code="scrcpy_install_failed"
            )
        return replace(installed, source="downloaded")

    def _managed(self) -> ScrcpyInstall | None:
        directory = self.install_directory
        if directory is None or not directory.is_dir():
            return None
        executable = self._locate(directory)
        if executable is None:
            return None
        try:
            install = self._inspect(executable, "managed", read_options=False)
        except DroidockError:
            return None
        return install if install.version == self.release.version else None

    def _locate(self, directory: Path) -> Path | None:
        name = "scrcpy.exe" if (self.platform or "").startswith("win") else "scrcpy"
        candidates = sorted(
            path
            for path in directory.rglob(name)
            if path.is_file() and len(path.relative_to(directory).parts) <= 3
        )
        if not candidates:
            return None
        executable = candidates[0]
        if os.name != "nt":
            mode = executable.stat().st_mode
            if not mode & stat.S_IXUSR:
                executable.chmod(mode | stat.S_IXUSR)
        return executable

    def _run(self, executable: Path, argument: str) -> str:
        try:
            result = self._runner(
                [str(executable), argument],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DroidockError(f"Cannot run scrcpy: {executable}\n{exc}", code="scrcpy_missing") from exc
        return f"{result.stdout or ''}\n{result.stderr or ''}"

    def _inspect(self, executable: Path, source: str, *, read_options: bool) -> ScrcpyInstall:
        version = parse_version(self._run(executable, "--version"))
        if version is None:
            raise DroidockError(f"Cannot read the scrcpy version of {executable}.", code="scrcpy_missing")
        options = parse_options(self._run(executable, "--help")) if read_options else frozenset()
        return ScrcpyInstall(executable.resolve(), version, source, options)

    def _download(self, asset: ScrcpyAsset, progress: ProgressCallback | None) -> Path:
        from . import __version__

        url = self.release.url(asset)
        failure: Exception | None = None
        for _ in range(DOWNLOAD_ATTEMPTS):
            handle, name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=self.directory)
            path = Path(name)
            digest = hashlib.sha256()
            received = 0
            try:
                request = urllib.request.Request(url, headers={"User-Agent": f"droidock/{__version__}"})
                with (
                    os.fdopen(handle, "wb") as stream,
                    self._opener(request, timeout=DOWNLOAD_TIMEOUT) as response,
                ):
                    while True:
                        chunk = response.read(1 << 16)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > asset.size:
                            break
                        stream.write(chunk)
                        digest.update(chunk)
                        if progress is not None:
                            progress(received, asset.size)
            except (urllib.error.URLError, OSError) as exc:
                path.unlink(missing_ok=True)
                failure = exc
                continue
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            if received != asset.size or digest.hexdigest() != asset.sha256:
                path.unlink(missing_ok=True)
                raise DroidockError(
                    f"The downloaded {asset.name} does not match its published SHA-256 digest and was deleted.",
                    code="scrcpy_checksum",
                )
            return path
        reason = getattr(failure, "reason", None) or failure
        raise DroidockError(
            f"Could not download scrcpy {self.release.version} ({url}): {reason}\n"
            "Connect to the internet once to install it, or install scrcpy 3.0 or later and set "
            "DROIDOCK_SCRCPY_PATH.",
            code="scrcpy_download_failed",
        )

    def _extract(self, archive: Path, asset: ScrcpyAsset, destination: Path) -> None:
        staging = Path(tempfile.mkdtemp(prefix=".extract-", dir=self.directory))
        root = staging.resolve()
        try:
            if asset.name.endswith(".zip"):
                with zipfile.ZipFile(archive) as bundle:
                    for member in bundle.infolist():
                        _require_inside(root, root / member.filename)
                    bundle.extractall(root)
            else:
                with tarfile.open(archive, "r:*") as bundle:
                    if hasattr(tarfile, "data_filter"):
                        bundle.extractall(root, filter="data")
                    else:  # Python 3.11 before 3.11.4
                        for member in bundle.getmembers():
                            if not (member.isfile() or member.isdir()):
                                raise DroidockError(
                                    "The scrcpy archive contains an unexpected link or device entry.",
                                    code="scrcpy_install_failed",
                                )
                            _require_inside(root, root / member.name)
                        bundle.extractall(root)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _require_inside(root: Path, candidate: Path) -> None:
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise DroidockError(
            "The scrcpy archive contains a path outside its folder; it was not installed.",
            code="scrcpy_install_failed",
        )
