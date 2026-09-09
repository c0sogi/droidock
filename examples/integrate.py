"""Example: uv run python examples/integrate.py [saved-device-name]

Reads a model property only. Uses the same persisted profiles as the CLI.
"""

from __future__ import annotations

import sys

import adbutils

from droidock import ConnectionEvent, ConnectionManager, DroidockError


def on_event(event: ConnectionEvent) -> None:
    print(f"{event.kind}: {event.message} {event.endpoint}")


def main() -> int:
    manager = ConnectionManager(on_event=on_event)
    try:
        transport = manager.resolve(sys.argv[1] if len(sys.argv) > 1 else None)
        adb = adbutils.AdbClient(host="127.0.0.1", port=manager.settings.server_port)
        device = adb.device(serial=transport.address)
        print(device.shell("getprop ro.product.model"))
    except DroidockError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
