from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

from filelock import FileLock, Timeout
from platformdirs import user_config_path

from .errors import DroidockError
from .models import DeviceRecord, Settings, State, valid_serial

T = TypeVar("T")


class DeviceStore:
    """Atomic, locked storage. Supply a directory to isolate an embedding application's profiles."""

    def __init__(self, directory: str | Path | None = None) -> None:
        configured = directory or os.environ.get("DROIDOCK_HOME")
        if configured:
            self.directory = Path(configured).expanduser().resolve()
        else:
            self.directory = user_config_path("droidock", appauthor=False)
        self.path = self.directory / "state.json"

    def _read(self) -> State:
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1:
                raise ValueError("Unsupported storage schema.")
            settings = Settings(**data["settings"])
            settings.validate()
            devices = [DeviceRecord(**item) for item in data["devices"]]
            for item in devices:
                if not all(
                    isinstance(x, str)
                    for x in (item.id, item.name, item.serial, item.last_seen, item.manufacturer, item.model)
                ):
                    raise ValueError("Invalid device details.")
                if not item.id or not item.name.strip() or not valid_serial(item.serial):
                    raise ValueError("Invalid device identity.")
                if not isinstance(item.auto_connect, bool):
                    raise ValueError("Invalid automatic connection setting.")
                if not all(
                    isinstance(x, list) and all(isinstance(v, str) for v in x)
                    for x in (item.endpoints, item.wifi_guids)
                ):
                    raise ValueError("Invalid saved connection addresses.")
            if len({d.id for d in devices}) != len(devices) or len({d.serial for d in devices}) != len(
                devices
            ):
                raise ValueError("Duplicate device identifiers were found.")
            default = data.get("default_device")
            if default is not None and default not in {d.id for d in devices}:
                raise ValueError("The default device is not registered.")
            return State(settings=settings, devices=devices, default_device=default)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DroidockError(
                f"Cannot read saved settings: {self.path}\n{exc}\nThe existing file will not be overwritten.",
                code="invalid_store",
            ) from exc

    def read(self) -> State:
        return self._read()  # Writers replace the whole file atomically.

    def update(self, operation: Callable[[State], T]) -> T:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.path) + ".lock", timeout=5):
                state = self._read()
                result = operation(state)
                state.settings.validate()
                handle, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.directory)
                temporary = Path(name)
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        json.dump(asdict(state), stream, ensure_ascii=False, indent=2)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
                return result
        except Timeout as exc:
            raise DroidockError("Another process is saving settings. Try again shortly.") from exc
        except OSError as exc:
            raise DroidockError(
                f"Cannot save connection settings: {self.path}\n{exc}", code="store_unwritable"
            ) from exc
