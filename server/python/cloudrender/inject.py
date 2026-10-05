"""InputInjector 抽象与 Windows 输入注入实现。

- ``WindowsInputInjector``:ctypes 直调 SendInput,零编译依赖;
- ``NativeCoreInjector``:走 C++ nativecore 的 cr_inject_* 系列。
"""
from __future__ import annotations

import abc
import ctypes
import logging
import threading
from ctypes import wintypes

from .capture import find_nativecore

logger = logging.getLogger("cloudrender.inject")


class InputInjector(abc.ABC):
    """把协议层解码出的事件注入 OS。"""

    def inject_key(self, vk: int, down: bool) -> None: ...
    def inject_mouse_move(self, x: int, y: int) -> None: ...
    def inject_mouse_move_rel(self, dx: int, dy: int) -> None: ...
    def inject_mouse_button(self, button: int, down: bool) -> None:
        raise NotImplementedError

    def inject_mouse_wheel(self, delta: int) -> None:
        raise NotImplementedError

    def inject_text(self, text: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ctypes SendInput
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MAPVK_VK_TO_VSC = 0

_EXTENDED_VKS = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28,
                 0xA3, 0xA5, 0x5B, 0x5C, 0x90, 0x6F}


class WindowsInputInjector(InputInjector):
    """SendInput 直调。运行于 Windows 时可用。"""

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._lock = threading.Lock()

    def _send(self, inputs) -> bool:
        n = len(inputs)
        array = (INPUT * n)(*inputs)
        return self._user32.SendInput(n, array, ctypes.sizeof(INPUT)) == n

    def inject_key(self, vk: int, down: bool) -> None:
        with self._lock:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki.wVk = vk
            inp.ki.wScan = self._user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
            flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
            if not down:
                flags |= KEYEVENTF_KEYUP
            inp.ki.dwFlags = flags
            self._send([inp])

    def inject_mouse_move(self, x: int, y: int) -> None:
        with self._lock:
            vl = self._user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN(虚拟屏左上 X)
            vt = self._user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN(虚拟屏左上 Y)
            vw = self._user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN(虚拟屏宽)
            vh = self._user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN(虚拟屏高)
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
            inp.mi.dx = int((x - vl) / max(1, vw) * 65535)
            inp.mi.dy = int((y - vt) / max(1, vh) * 65535)
            self._send([inp])

    def inject_mouse_move_rel(self, dx: int, dy: int) -> None:
        with self._lock:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.mi.dwFlags = MOUSEEVENTF_MOVE
            inp.mi.dx, inp.mi.dy = dx, dy
            self._send([inp])

    def inject_mouse_button(self, button: int, down: bool) -> None:
        flags = {0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                 1: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
                 2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)}.get(button)
        if not flags:
            return
        with self._lock:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.mi.dwFlags = flags[0 if down else 1]
            self._send([inp])

    def inject_mouse_wheel(self, delta: int) -> None:
        with self._lock:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.mi.dwFlags = MOUSEEVENTF_WHEEL
            inp.mi.mouseData = delta & 0xFFFFFFFF
            self._send([inp])

    def inject_text(self, text: str) -> None:
        with self._lock:
            for ch in text:
                pair = []
                for up in (False, True):
                    inp = INPUT()
                    inp.type = INPUT_KEYBOARD
                    inp.ki.wScan = ord(ch)
                    inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
                    pair.append(inp)
                self._send(pair)


# ---------------------------------------------------------------------------
# nativecore 桥
# ---------------------------------------------------------------------------

class NativeCoreInjector(InputInjector):
    """cr_inject_* 系列。nativecore.dll 缺失时抛 RuntimeError。"""

    def __init__(self):
        self._lib = find_nativecore()
        if self._lib is None:
            raise RuntimeError("nativecore.dll not found (use WindowsInputInjector instead)")
        for name, argtypes in {
            "cr_inject_create": [], "cr_inject_destroy": [ctypes.c_void_p],
            "cr_inject_key": [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_int],
            "cr_inject_mouse_move": [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32],
            "cr_inject_mouse_move_rel": [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32],
            "cr_inject_mouse_button": [ctypes.c_void_p, ctypes.c_int, ctypes.c_int],
            "cr_inject_mouse_wheel": [ctypes.c_void_p, ctypes.c_int32],
            "cr_inject_text": [ctypes.c_void_p, ctypes.c_char_p],
        }.items():
            getattr(self._lib, name).argtypes = argtypes
        self._lib.cr_inject_create.restype = ctypes.c_void_p
        self._handle = self._lib.cr_inject_create()
        self._lock = threading.Lock()

    def _call(self, name, *args) -> int:
        return int(getattr(self._lib, name)(self._handle, *args))

    def inject_key(self, vk: int, down: bool) -> None:
        with self._lock:
            self._call("cr_inject_key", vk, 1 if down else 0)

    def inject_mouse_move(self, x: int, y: int) -> None:
        with self._lock:
            self._call("cr_inject_mouse_move", x, y)

    def inject_mouse_move_rel(self, dx: int, dy: int) -> None:
        with self._lock:
            self._call("cr_inject_mouse_move_rel", dx, dy)

    def inject_mouse_button(self, button: int, down: bool) -> None:
        with self._lock:
            self._call("cr_inject_mouse_button", button, 1 if down else 0)

    def inject_mouse_wheel(self, delta: int) -> None:
        with self._lock:
            self._call("cr_inject_mouse_wheel", delta)

    def inject_text(self, text: str) -> None:
        with self._lock:
            self._call("cr_inject_text", text.encode("utf-8"))

    def close(self) -> None:
        if getattr(self, "_lib", None):
            self._lib.cr_inject_destroy(self._handle)
            self._handle = None