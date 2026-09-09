"""Example: uv run python examples/integrate.py [saved-device-name]

Reads a model property only. Uses the same persisted profiles as the CLI.
"""

from __future__ import annotations

import sys

from droidock import ConnectionEvent, ConnectionManager, DroidockError


def on_event(event: ConnectionEvent) -> None:
    print(f"{event.kind}: {event.message} {event.endpoint}")


def main() -> int:
    manager = ConnectionManager(on_event=on_event)
    try:
        transport = manager.ensure_connected(sys.argv[1] if len(sys.argv) > 1 else None)
        result = manager.run(["shell", "getprop", "ro.product.model"], device=transport)
        print(result.stdout.strip())
    except DroidockError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
