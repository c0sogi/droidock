"""Keep the mirror window where the user put it when scrcpy restarts (Windows only).

Every call is best effort: a failure leaves the new window where scrcpy opened it.
Coordinates are read and written in physical pixels, so mixed display scaling is handled
without knowing how SDL scales its own window coordinates.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_MAXIMIZE = 3
MONITOR_DEFAULTTONEAREST = 2
PER_MONITOR_AWARE_V2 = -4


@dataclass(frozen=True)
class WindowPlacement:
    left: int
    top: int
    width: int
    height: int
    fullscreen: bool = False
    maximized: bool = False


class WindowKeeper(Protocol):
    def capture(self, pid: int) -> WindowPlacement | None: ...

    def restore(self, pid: int, placement: WindowPlacement, *, timeout: float = 15.0) -> bool: ...


def window_keeper() -> WindowKeeper | None:
    """The platform window helper, or None where windows cannot be placed."""
    if os.name != "nt":
        return None
    try:
        return Win32WindowKeeper()
    except (AttributeError, OSError, ImportError):
        return None


class Win32WindowKeeper:
    def __init__(self) -> None:
        import ctypes as ctypes_module
        from ctypes import wintypes as wintypes_module

        # Windows-only ctypes members are absent from other platforms' type stubs.
        ctypes: Any = ctypes_module
        wintypes: Any = wintypes_module

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._monitor_info = MonitorInfo
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures: dict[str, tuple[list[Any], Any]] = {
            "EnumWindows": ([self._enum_proc, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "MonitorFromWindow": ([wintypes.HWND, wintypes.DWORD], wintypes.HMONITOR),
            "GetMonitorInfoW": ([wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
            "SetWindowPos": (
                [
                    wintypes.HWND,
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ],
                wintypes.BOOL,
            ),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(user32, name)
            function.argtypes = arguments
            function.restype = result
        self._set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if self._set_dpi is not None:
            self._set_dpi.argtypes = [wintypes.HANDLE]
            self._set_dpi.restype = wintypes.HANDLE

    @contextmanager
    def _physical_pixels(self) -> Iterator[None]:
        # Per-thread only: the process and system DPI settings are not changed.
        set_dpi = self._set_dpi
        previous = set_dpi(self._wintypes.HANDLE(PER_MONITOR_AWARE_V2)) if set_dpi is not None else None
        try:
            yield
        finally:
            if set_dpi is not None and previous:
                set_dpi(previous)

    def _windows(self, pid: int) -> list[Any]:
        found: list[Any] = []
        wintypes = self._wintypes

        def collect(hwnd: Any, _: Any) -> bool:
            owner = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(owner))
            if owner.value == pid and self._user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        self._user32.EnumWindows(self._enum_proc(collect), 0)
        return found

    def _rect(self, hwnd: Any) -> tuple[int, int, int, int] | None:
        rect = self._wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def _main_window(self, pid: int) -> Any | None:
        def area(hwnd: Any) -> int:
            rect = self._rect(hwnd)
            return (rect[2] - rect[0]) * (rect[3] - rect[1]) if rect else 0

        windows = self._windows(pid)
        return max(windows, key=area) if windows else None

    def _monitor(self, hwnd: Any) -> tuple[int, int, int, int] | None:
        monitor = self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = self._monitor_info()
        info.cbSize = self._ctypes.sizeof(info)
        if not self._user32.GetMonitorInfoW(monitor, self._ctypes.byref(info)):
            return None
        bounds = info.rcMonitor
        return bounds.left, bounds.top, bounds.right, bounds.bottom

    def capture(self, pid: int) -> WindowPlacement | None:
        try:
            with self._physical_pixels():
                hwnd = self._main_window(pid)
                rect = self._rect(hwnd) if hwnd is not None else None
                if hwnd is None or rect is None:
                    return None
                maximized = bool(self._user32.IsZoomed(hwnd))
                fullscreen = not maximized and rect == self._monitor(hwnd)
        except OSError:
            return None
        left, top, right, bottom = rect
        return WindowPlacement(left, top, right - left, bottom - top, fullscreen, maximized)

    def restore(self, pid: int, placement: WindowPlacement, *, timeout: float = 15.0) -> bool:
        """Wait for the process's window to appear, then move it to the saved placement."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                with self._physical_pixels():
                    hwnd = self._main_window(pid)
                    if hwnd is not None:
                        # scrcpy sizes its window before showing it; place it twice in case
                        # the first move raced with that initial sizing.
                        moved = False
                        for attempt in range(2):
                            if attempt:
                                time.sleep(0.3)
                            moved = self._user32.SetWindowPos(
                                hwnd,
                                None,
                                placement.left,
                                placement.top,
                                placement.width,
                                placement.height,
                                SWP_NOZORDER | SWP_NOACTIVATE,
                            )
                        if placement.maximized:
                            self._user32.ShowWindow(hwnd, SW_MAXIMIZE)
                        return bool(moved)
                time.sleep(0.1)
        except OSError:
            return False
        return False
