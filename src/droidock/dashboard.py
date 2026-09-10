"""Scrollable, selectable snapshot tables. This view never performs device I/O."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, ScrollOffsets, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from . import __version__
from .display import _connection_addresses, _connection_status, _device_groups, _service_connected
from .models import DeviceRecord, ServiceGroup, Snapshot, Transport


@dataclass(frozen=True)
class DeviceEntry:
    record: DeviceRecord | None
    transports: tuple[Transport, ...]
    conflict: str = ""

    @property
    def key(self) -> str:
        return "profile:" + self.record.id if self.record else "transport:" + self.transports[0].address

    @property
    def label(self) -> str:
        if self.record:
            return self.record.name
        identity = self.transports[0].identity
        return identity.model or "Android" if identity else "Unidentified device"


@dataclass(frozen=True)
class DashboardAction:
    name: str
    device: DeviceEntry | None = None
    service: ServiceGroup | None = None


def _cell(value: str, width: int) -> str:
    """Fit literal text to terminal cells, including wide Unicode and emoji."""
    value = " ".join(value.split())
    if get_cwidth(value) > width:
        while value and get_cwidth(value) > max(0, width - 1):
            value = value[:-1]
        value += "…" if width else ""
    return value + " " * max(0, width - get_cwidth(value))


class Dashboard:
    def __init__(self, snapshot: Snapshot, default_device: str | None = None) -> None:
        self.snapshot = snapshot
        self.default_device = default_device
        self.devices: list[DeviceEntry] = []
        self.services: list[ServiceGroup] = []
        self.selected = [0, 0]
        self.controls = [self._control(0), self._control(1)]
        self.windows = [
            Window(
                control,
                height=Dimension(min=1, weight=1),
                wrap_lines=False,
                right_margins=[ScrollbarMargin(display_arrows=True)],
                scroll_offsets=ScrollOffsets(top=1, bottom=1),
                always_hide_cursor=True,
            )
            for control in self.controls
        ]
        root = HSplit(
            [
                Window(
                    FormattedTextControl(f"Droidock {__version__}  ·  Device connections"),
                    height=1,
                    style="bold",
                ),
                Window(height=1),
                self._pane(0),
                Window(height=1),
                self._pane(1),
                Window(height=1),
                Window(FormattedTextControl(self._details), height=2, wrap_lines=True, style="class:details"),
                Window(FormattedTextControl(self._warnings), height=1, style="class:warning"),
                Window(
                    FormattedTextControl(
                        "Tab: table  ↑↓/PgUp/PgDn: scroll  Enter: actions  R/F5: scan  A: add  F2: menu  Q: exit"
                    ),
                    height=2,
                    wrap_lines=True,
                    style="class:help",
                ),
            ],
            height=lambda: max(1, get_app().output.get_size().rows - 1),
        )
        self.app: Application[DashboardAction | None] = Application(
            layout=Layout(root, focused_element=self.controls[0]),
            key_bindings=self._keys(),
            full_screen=False,
            erase_when_done=True,
            mouse_support=True,
            style=Style.from_dict(
                {
                    "devices": "ansicyan bold",
                    "services": "ansimagenta bold",
                    "selected.devices": "bg:ansicyan ansiblack",
                    "selected.services": "bg:ansimagenta ansiblack",
                    "details": "italic",
                    "help": "reverse",
                    "warning": "ansiyellow",
                    "scrollbar.background": "",
                    "scrollbar.button": "bg:ansibrightblack",
                }
            ),
        )
        self.update(snapshot, default_device)

    def _control(self, pane: int) -> FormattedTextControl:
        return FormattedTextControl(
            lambda: self._rows(pane),
            focusable=True,
            show_cursor=False,
            get_cursor_position=lambda: Point(x=0, y=self.selected[pane]),
        )

    def _pane(self, pane: int) -> HSplit:
        style = "class:devices" if pane == 0 else "class:services"
        return HSplit(
            [
                Window(FormattedTextControl(lambda: self._title(pane)), height=1, style=style),
                Window(FormattedTextControl(lambda: self._header(pane)), height=1, style=style),
                Window(height=1, char="━" if pane == 0 else "─", style=style),
                self.windows[pane],
            ]
        )

    def _focus(self) -> int:
        return 0 if self.app.layout.current_control is self.controls[0] else 1

    def _count(self, pane: int) -> int:
        return len(self.devices) if pane == 0 else len(self.services)

    def _title(self, pane: int) -> str:
        title = "📱 Devices" if pane == 0 else "📡 Wireless services"
        marker = "▶ " if self._focus() == pane else "  "
        return f"{marker}{title}  ({self.selected[pane] + 1 if self._count(pane) else 0}/{self._count(pane)})"

    def _columns(self, cells: tuple[str, str, str, str]) -> str:
        width = max(12, self.app.output.get_size().columns - 4)
        widths = [int(width * ratio) for ratio in (0.32, 0.14, 0.24)]
        widths.append(max(1, width - sum(widths)))
        return " ".join(_cell(value, size) for value, size in zip(cells, widths, strict=True))

    def _header(self, pane: int) -> str:
        return self._columns(
            ("Device / alias", "Saved", "Status", "Connections")
            if pane == 0
            else ("Service name", "Purpose", "Status", "Addresses")
        )

    def _rows(self, pane: int) -> StyleAndTextTuples:
        if not self._count(pane):
            return [
                (
                    "",
                    "No saved devices or detected ADB connections."
                    if pane == 0
                    else "No wireless debugging services discovered.",
                )
            ]
        lines: StyleAndTextTuples = []
        for index in range(self._count(pane)):
            if pane == 0:
                item = self.devices[index]
                saved = (
                    "Yes"
                    if item.record
                    else "No"
                    if item.transports[0].identity and not item.conflict
                    else "Unverified"
                )
                label = item.label + (
                    " (default)" if item.record and item.record.id == self.default_device else ""
                )
                status = (
                    "🟡 Identity conflict" if item.conflict else _connection_status(item.transports).plain
                )
                addresses = _connection_addresses(item.transports).replace("\n", " | ")
                if not addresses and item.record:
                    addresses = (
                        "Last: " + ", ".join(item.record.endpoints)
                        if item.record.endpoints
                        else "No current connection"
                    )
                cells = (label, saved, status, addresses)
            else:
                service = self.services[index]
                status = (
                    "🟢 Connected"
                    if _service_connected(service, self.snapshot.transports)
                    else "🔎 Discovered"
                )
                cells = (
                    service.instance or "Unnamed service",
                    service.kind.value.capitalize(),
                    status,
                    ", ".join(service.endpoints),
                )
            selected = self._focus() == pane and index == self.selected[pane]
            style = ("class:selected.devices" if pane == 0 else "class:selected.services") if selected else ""
            lines.append((style, self._columns(cells), self._mouse(pane, index)))
            if index + 1 < self._count(pane):
                lines.append(("", "\n"))
        return lines

    def _mouse(self, pane: int, index: int):
        def handle(event: MouseEvent) -> None:
            self.app.layout.focus(self.controls[pane])
            if event.event_type == MouseEventType.MOUSE_UP:
                self.selected[pane] = index
            elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                self._move(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
            self.app.invalidate()

        return handle

    def _details(self) -> str:
        pane = self._focus()
        if not self._count(pane):
            return "Press A to pair or enter an address. Press R to scan again."
        if pane == 1:
            service = self.services[self.selected[1]]
            return f"{service.instance} | " + " | ".join(service.endpoints)
        device = self.devices[self.selected[0]]
        identity = next((t.identity for t in device.transports if t.identity), None)
        serial = device.record.serial if device.record else identity.serial if identity else "unverified"
        auto = ("enabled" if device.record.auto_connect else "disabled") if device.record else "not saved"
        return f"{device.label} | Serial: {serial} | Auto-connect: {auto}\n" + (
            device.conflict or _connection_addresses(device.transports).replace("\n", " | ")
        )

    def _warnings(self) -> str:
        return (
            " | ".join(self.snapshot.warnings)
            if self.snapshot.warnings
            else "Wireless services may be discovered without an ADB connection."
        )

    def _move(self, amount: int) -> None:
        pane = self._focus()
        self.selected[pane] = min(max(0, self._count(pane) - 1), max(0, self.selected[pane] + amount))

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("tab")
        @keys.add("s-tab")
        def switch(event: KeyPressEvent) -> None:
            event.app.layout.focus(self.controls[1 - self._focus()])

        @keys.add("up")
        @keys.add("down")
        @keys.add("pageup")
        @keys.add("pagedown")
        @keys.add("home")
        @keys.add("end")
        def move(event: KeyPressEvent) -> None:
            key = event.key_sequence[-1].key
            info = self.windows[self._focus()].render_info
            page = max(1, info.window_height - 1) if info else 5
            amounts = {
                "up": -1,
                "down": 1,
                "pageup": -page,
                "pagedown": page,
                "home": -self._count(self._focus()),
                "end": self._count(self._focus()),
            }
            self._move(amounts[key])

        @keys.add("enter")
        def select(event: KeyPressEvent) -> None:
            pane = self._focus()
            if self._count(pane):
                event.app.exit(
                    result=(
                        DashboardAction("device", device=self.devices[self.selected[0]])
                        if pane == 0
                        else DashboardAction("service", service=self.services[self.selected[1]])
                    )
                )

        for key, action in (("r", "scan"), ("f5", "scan"), ("a", "register"), ("f2", "menu")):

            def shortcut(event: KeyPressEvent, action: str = action) -> None:
                event.app.exit(result=DashboardAction(action))

            keys.add(key)(shortcut)

        @keys.add("q")
        @keys.add("escape")
        @keys.add("c-c")
        @keys.add("c-d")
        def exit_menu(event: KeyPressEvent) -> None:
            event.app.exit(result=None)

        return keys

    def update(self, snapshot: Snapshot, default_device: str | None = None) -> None:
        old_keys = [
            self.devices[self.selected[0]].key if self.devices else None,
            self.services[self.selected[1]] if self.services else None,
        ]
        self.snapshot, self.default_device = snapshot, default_device
        assigned, unknown = _device_groups(snapshot)
        self.devices = [DeviceEntry(record, tuple(assigned[record.id])) for record in snapshot.devices]
        self.devices.extend(DeviceEntry(None, group.transports, group.conflict) for group in unknown)
        self.services = snapshot.service_groups
        new_keys: list[list[object]] = [[item.key for item in self.devices], list(self.services)]
        for pane in (0, 1):
            self.selected[pane] = (
                new_keys[pane].index(old_keys[pane])
                if old_keys[pane] in new_keys[pane]
                else min(self.selected[pane], max(0, self._count(pane) - 1))
            )

    def run(self) -> DashboardAction | None:
        return self.app.run()
