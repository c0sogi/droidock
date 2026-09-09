from __future__ import annotations

import functools
import io
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from . import __version__
from .display import (
    show_auto_connect,
    show_device,
    show_diagnostics,
    show_disconnected,
    show_pairing,
    show_settings,
    show_snapshot,
)
from .errors import DroidockError
from .interactive import InteractiveCli
from .manager import ConnectionManager
from .store import DeviceStore

app = typer.Typer(
    add_completion=False,
    help="Save Android device profiles, discover devices, pair, and reconnect automatically.",
)
console = Console()
JsonOutput = Annotated[bool, typer.Option("--json", help="Print structured JSON for scripts.")]


def guarded(function: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except DroidockError as exc:
            if kwargs.get("json_output"):
                typer.echo(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
            else:
                Console(stderr=True).print(str(exc), style="red", markup=False)
            raise typer.Exit(1) from exc

    return wrapper


def manager(ctx: typer.Context) -> ConnectionManager:
    if ctx.obj is None:
        ctx.obj = ConnectionManager(DeviceStore(ctx.find_root().params.get("data_dir")))
    return ctx.obj


def emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def show_version(value: bool) -> None:
    if value:
        typer.echo(f"droidock {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
@guarded
def root(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option(help="Directory for saved device profiles.", envvar="DROIDOCK_HOME")
    ] = None,
    plain: Annotated[bool, typer.Option(help="Use numbered menus instead of arrow keys.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=show_version, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        InteractiveCli(manager(ctx), plain=plain).run()


@app.command()
@guarded
def devices(ctx: typer.Context, json_output: JsonOutput = False) -> None:
    """List current connections and nearby wireless devices without requesting reconnection."""
    current = manager(ctx)
    snapshot = current.scan()
    if json_output:
        emit(asdict(snapshot))
    else:
        show_snapshot(console, snapshot, current.store.read().default_device)


@app.command()
@guarded
def register(
    ctx: typer.Context,
    address: Annotated[
        str, typer.Argument(help="USB serial or wireless address of an already connected device.")
    ],
    name: Annotated[str | None, typer.Option(help="Name for the saved device.")] = None,
    json_output: JsonOutput = False,
) -> None:
    """Verify a connected device and save its identity on this PC."""
    current = manager(ctx)
    record = current.register(address, name=name)
    if json_output:
        emit(asdict(record))
    else:
        show_device(
            console, record, title="Device registered", default_device=current.store.read().default_device
        )


@app.command()
@guarded
def connect(
    ctx: typer.Context,
    device: Annotated[
        str | None,
        typer.Argument(help="Saved name, device ID, serial, or address. Omit to select automatically."),
    ] = None,
    endpoint: Annotated[
        str | None, typer.Option(help="Current connection IP:port, separate from the pairing port.")
    ] = None,
    name: Annotated[str | None, typer.Option(help="Name to save or update for the selected device.")] = None,
    json_output: JsonOutput = False,
) -> None:
    """Connect to the selected device and verify its identity."""
    current = manager(ctx)
    transport = current.ensure_connected(device, endpoint=endpoint, name=name)
    assert transport.identity is not None
    record = current.device(transport.identity.serial)
    if json_output:
        emit(asdict(record))
    else:
        show_device(
            console, record, title="Connection verified", default_device=current.store.read().default_device
        )


@app.command()
@guarded
def pair(
    ctx: typer.Context,
    endpoint: Annotated[str | None, typer.Argument(help="IP:port shown on the pairing screen.")] = None,
    code_stdin: Annotated[
        bool, typer.Option(help="Read the six-digit code from standard input instead of a command argument.")
    ] = False,
    json_output: JsonOutput = False,
) -> None:
    """Pair wirelessly. Omit the address to open the pairing and registration wizard."""
    if json_output and (not endpoint or not code_stdin):
        raise DroidockError("Specify a pairing address and use --code-stdin with --json.")
    if not endpoint:
        if code_stdin:
            raise DroidockError("Specify a pairing address when using --code-stdin.")
        InteractiveCli(manager(ctx)).pair()
        return
    if code_stdin:
        code = sys.stdin.readline().strip()
    else:
        code = typer.prompt("Six-digit pairing code shown on the device", hide_input=True)
    result = manager(ctx).pair(endpoint, code)
    if json_output:
        emit(
            {"status": "paired", **asdict(result), "next": "connect --endpoint <current-connection-IP:port>"}
        )
    else:
        show_pairing(console, result)


@app.command("auto-connect")
@guarded
def auto_connect(ctx: typer.Context, json_output: JsonOutput = False) -> None:
    """Try connecting once to each saved device with automatic connection enabled."""
    current = manager(ctx)
    result = current.auto_connect()
    if json_output:
        emit(asdict(result))
    else:
        show_auto_connect(console, result, current.store.read().devices)
    if result.errors:
        raise typer.Exit(1)


@app.command()
@guarded
def watch(
    ctx: typer.Context,
    interval: Annotated[float, typer.Option(min=1, help="Discovery interval in seconds.")] = 5,
    once: Annotated[
        bool, typer.Option(help="Run automatic connection once and show the resulting status.")
    ] = False,
) -> None:
    """Discover and reconnect saved devices while running. Press Ctrl+C to stop."""
    current = manager(ctx)
    for snapshot in current.watch(interval=interval):
        show_snapshot(console, snapshot, current.store.read().default_device)
        if once:
            break


@app.command()
@guarded
def diagnose(ctx: typer.Context, json_output: JsonOutput = False) -> None:
    """Show ADB, server, and device diagnostics with connection guidance."""
    report = manager(ctx).diagnostics()
    if json_output:
        emit(report)
    else:
        show_diagnostics(console, report)


@app.command("restart-server")
@guarded
def restart_server(
    ctx: typer.Context,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm interruption of all clients on this ADB server.")
    ] = False,
    json_output: JsonOutput = False,
) -> None:
    """Restart the selected local ADB server and reconnect saved auto-connect devices."""
    current = manager(ctx)
    port = current.settings.server_port
    if not yes:
        if json_output:
            raise DroidockError(
                "Pass --yes to confirm the server restart with --json.", code="confirmation_required"
            )
        if not typer.confirm(
            f"Restart ADB server on port {port}? All apps using it will be disconnected.", default=False
        ):
            return
    report = current.restart_server()
    if json_output:
        emit({"server_restarted": True, "server_port": port, **asdict(report)})
    else:
        console.print(f"ADB server on port {port} restarted.", style="green")
        show_auto_connect(console, report, current.store.read().devices)
    if report.errors:
        raise typer.Exit(1)


@app.command()
@guarded
def settings(
    ctx: typer.Context,
    key: Annotated[str | None, typer.Argument(help="Setting name. Omit to show current settings.")] = None,
    value: Annotated[str | None, typer.Argument(help="Value to save.")] = None,
    json_output: JsonOutput = False,
) -> None:
    """View or update settings. Example: settings auto_connect_on_start false"""
    current = manager(ctx)
    if key is not None:
        values = asdict(current.settings)
        if key not in values or value is None:
            raise DroidockError("Check the setting name and value. Available settings: " + ", ".join(values))
        existing = values[key]
        try:
            if isinstance(existing, bool):
                if value.lower() not in {"true", "false"}:
                    raise ValueError
                parsed: object = value.lower() == "true"
            elif isinstance(existing, int):
                parsed = int(value)
            elif isinstance(existing, float):
                parsed = float(value)
            else:
                parsed = "" if value == "auto" else value
        except ValueError as exc:
            raise DroidockError("Invalid value format. Use true or false for boolean settings.") from exc
        current.configure(**{key: parsed})
    if json_output:
        emit({"store": str(current.store.path), **asdict(current.settings)})
    else:
        if key is not None:
            console.print("Settings saved.", style="green")
        show_settings(console, current.settings, current.store.path)


@app.command()
@guarded
def profile(
    ctx: typer.Context,
    device: str,
    name: str | None = None,
    auto_connect: Annotated[bool | None, typer.Option("--auto-connect/--no-auto-connect")] = None,
    default: bool = False,
    json_output: JsonOutput = False,
) -> None:
    """Update a saved device name, automatic connection preference, or default selection."""
    current = manager(ctx)
    record = current.update_device(device, name=name, auto_connect=auto_connect, default=default)
    if json_output:
        emit(asdict(record))
    else:
        show_device(console, record, default_device=current.store.read().default_device)


@app.command()
@guarded
def disconnect(ctx: typer.Context, device: str, json_output: JsonOutput = False) -> None:
    """Disconnect verified wireless connections for this device and disable automatic connection."""
    count = manager(ctx).disconnect(device)
    if json_output:
        emit({"disconnected": count})
    else:
        show_disconnected(console, count)


@app.command()
@guarded
def forget(ctx: typer.Context, device: str, yes: bool = False) -> None:
    """Delete the saved profile on this PC. Android pairing authorization is retained."""
    if not yes and not typer.confirm("Delete this device profile from the PC?"):
        raise typer.Abort()
    manager(ctx).forget(device)
    typer.echo("Device profile deleted.")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    try:
        app()
    except (KeyboardInterrupt, EOFError):
        return
