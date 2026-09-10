from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from droidock import DeviceRecord, Identity, Service, ServiceGroup, ServiceKind, Snapshot, Transport
from droidock.dashboard import Dashboard, DeviceEntry
from droidock.interactive import InteractiveCli


def large_snapshot() -> Snapshot:
    return Snapshot(
        [DeviceRecord(str(i), f"Phone {i:02}", f"SERIAL-{i:02}") for i in range(40)],
        [],
        [Service(f"service-{i:02}", ServiceKind.CONNECT, f"192.0.2.{i + 1}:40001") for i in range(40)],
    )


class ResizableOutput(DummyOutput):
    size = Size(rows=26, columns=100)

    def get_size(self) -> Size:
        return self.size


def test_both_tables_scroll_follow_selection_and_resize():
    async def exercise():
        output = ResizableOutput()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            dashboard = Dashboard(large_snapshot())
            task = asyncio.create_task(dashboard.app.run_async())

            async def wait_for_render(predicate):
                async with asyncio.timeout(3):
                    while not predicate():
                        await asyncio.sleep(0.01)

            try:
                await wait_for_render(lambda: dashboard.windows[0].render_info is not None)
                pipe.send_text("\x1b[F")  # End: select the last device.
                await wait_for_render(lambda: dashboard.windows[0].vertical_scroll > 0)
                assert dashboard.selected[0] == 39
                info = dashboard.windows[0].render_info
                assert info and 39 in info.displayed_lines
                assert info.window_height < 40

                pipe.send_text("\t\x1b[F")
                await wait_for_render(lambda: dashboard.windows[1].vertical_scroll > 0)
                assert dashboard.selected == [39, 39]
                output.size = Size(rows=18, columns=70)
                dashboard.app.invalidate()
                await wait_for_render(
                    lambda: (
                        bool(dashboard.windows[1].render_info)
                        and dashboard.windows[1].render_info.window_height <= 3
                    )
                )
                info = dashboard.windows[1].render_info
                assert info and 39 in info.displayed_lines
                pipe.send_text("\r")
                result = await asyncio.wait_for(task, 3)
                assert result and result.service and result.service.instance == "service-39"
            finally:
                if not task.done():
                    dashboard.app.exit()
                    await task

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("keys", "expected"),
    [("r", "scan"), ("\x1b[15~", "scan"), ("a", "register"), ("\x1bOQ", "menu"), ("q", None), ("\x03", None)],
)
def test_dashboard_shortcuts_work_without_device_io(keys, expected):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        dashboard = Dashboard(Snapshot([], [], []))
        pipe.send_text(keys)
        result = dashboard.run()
    assert (result.name if result else None) == expected


def test_selection_survives_refresh_and_removed_rows_are_clamped():
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        snapshot = large_snapshot()
        dashboard = Dashboard(snapshot)
        dashboard.selected = [10, 39]
        dashboard.update(Snapshot(list(reversed(snapshot.devices)), [], snapshot.services[:2]))
        selected_record = dashboard.devices[dashboard.selected[0]].record
        assert selected_record is not None and selected_record.id == "10"
        assert dashboard.selected[1] == 1
        dashboard.update(Snapshot([], [], []))
        pipe.send_text("\rq")  # Enter on an empty table must not open an action.
        assert dashboard.run() is None


def test_cached_usb_registration_does_not_rescan(rig):
    manager, _, _ = rig
    menu = InteractiveCli(manager, plain=True)
    menu._scan()
    entry = DeviceEntry(None, tuple(menu._snapshot.transports))
    with (
        patch.object(menu, "choose", return_value="save"),
        patch.object(menu, "text", return_value="Office phone"),
        patch.object(manager, "scan", side_effect=AssertionError("Unexpected discovery")),
    ):
        menu._device_action(entry)
    assert manager.device("Office phone").serial == "SERIAL-A"


def test_selected_service_connects_cached_address_without_scan(rig):
    manager, backend, _ = rig
    address = "192.0.2.10:40001"
    backend.network[address] = Transport(address, "device", Identity("DEVICE-A"))
    menu = InteractiveCli(manager, plain=True)
    with (
        patch.object(menu, "choose", return_value="connect"),
        patch.object(menu, "text", return_value="Wireless phone"),
        patch.object(manager, "scan", side_effect=AssertionError("Unexpected discovery")),
    ):
        menu._service_action(ServiceGroup("selected", ServiceKind.CONNECT, (address,)))
    assert backend.connected == [address]
    assert manager.device("Wireless phone").serial == "DEVICE-A"


def test_failed_address_refreshes_only_selected_service(rig):
    manager, backend, discovery = rig
    old, current, unrelated = "192.0.2.10:40001", "192.0.2.10:40002", "192.0.2.11:40002"
    backend.network[current] = Transport(current, "device", Identity("DEVICE-A"))
    discovery.services = [
        Service("selected", ServiceKind.CONNECT, current),
        Service("different", ServiceKind.CONNECT, unrelated),
    ]
    menu = InteractiveCli(manager, plain=True)
    with (
        patch.object(menu, "choose", return_value="connect"),
        patch.object(menu, "text", return_value="Wireless phone"),
        patch.object(manager, "scan", wraps=manager.scan) as scan,
    ):
        menu._service_action(ServiceGroup("selected", ServiceKind.CONNECT, (old,)))
    assert scan.call_count == 1
    assert backend.connected == [old, current]


def test_opening_add_menu_uses_startup_snapshot(rig, monkeypatch):
    manager, _, _ = rig
    answers = iter(["3", "1", "1", "Office phone", "0"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    with patch.object(manager, "scan", wraps=manager.scan) as scan:
        InteractiveCli(manager, plain=True).run()
    assert scan.call_count == 1
