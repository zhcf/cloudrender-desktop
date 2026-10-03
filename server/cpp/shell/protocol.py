"""CloudRender 协议编解码:信令常量、FNV-1a 键码、输入/事件二进制帧。

协议规范见 docs/protocol.md。本模块是 Python 服务端与客户端共用的唯一协议实现,
与 JS/C++ 版本保持字节级一致。

(cpp shell 副本:源 server/python/cloudrender/protocol.py,修改须两处同步)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

PROTOCOL_VERSION = 1
MAGIC_INPUT = b"CRIN"
MAGIC_EVENT = b"CREV"

# ---------- 信令 ----------

# 错误码
ERR_OK = 0
ERR_UNAUTHORIZED = 4001
ERR_MAX_SESSIONS = 4002
ERR_BAD_PARAM = 4003
ERR_NEGOTIATE = 4100
ERR_ICE_TIMEOUT = 4101
ERR_ENCODER = 4102
ERR_INTERNAL = 5000


def make_message(msg_type: str, seq: int = 0, **payload) -> dict:
    msg = {"type": msg_type, "seq": seq}
    if payload:
        msg["payload"] = payload
    return msg


# ---------- FNV-1a/32 键码(协议 §3.1) ----------

def fnv1a32(s: str) -> int:
    """FNV-1a 32bit,offset basis 0x811C9DC5,prime 0x01000193。"""
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


# W3C KeyboardEvent.code → Windows VK。键码哈希不可逆,服务端用该表反查 code 再转 VK。
CODE_TO_VK: dict = {}
for _i, _ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    CODE_TO_VK[f"Key{_ch}"] = 0x41 + _i
for _i in range(10):
    CODE_TO_VK[f"Digit{_i}"] = 0x30 + _i
for _i in range(1, 13):
    CODE_TO_VK[f"F{_i}"] = 0x70 + _i - 1
CODE_TO_VK.update({
    # 主键盘区
    "Backquote": 0xC0, "Minus": 0xBD, "Equal": 0xBB, "Backspace": 0x08,
    "Tab": 0x09, "BracketLeft": 0xDB, "BracketRight": 0xDD, "Backslash": 0xDC,
    "CapsLock": 0x14, "Semicolon": 0xBA, "Quote": 0xDE, "Enter": 0x0D,
    "ShiftLeft": 0xA0, "ShiftRight": 0xA1,
    "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "ControlLeft": 0xA2, "ControlRight": 0xA3,
    "AltLeft": 0xA4, "AltRight": 0xA5,
    "MetaLeft": 0x5B, "MetaRight": 0x5C, "Space": 0x20, "ContextMenu": 0x5D,
    "Escape": 0x1B, "Enter": 0x0D,
    # 导航/编辑
    "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25, "ArrowRight": 0x27,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Insert": 0x2D, "Delete": 0x2E,
    # 小键盘
    "Numpad0": 0x60, "Numpad1": 0x61, "Numpad2": 0x62, "Numpad3": 0x63,
    "Numpad4": 0x64, "Numpad5": 0x65, "Numpad6": 0x66, "Numpad7": 0x67,
    "Numpad8": 0x68, "Numpad9": 0x69,
    "NumpadAdd": 0x6B, "NumpadSubtract": 0x6D, "NumpadMultiply": 0x6A,
    "NumpadDivide": 0x6F, "NumpadDecimal": 0x6E, "NumpadEnter": 0x0D,
    "NumLock": 0x90, "ScrollLock": 0x91, "Pause": 0x13, "PrintScreen": 0x2C,
})

# fnv → code,用于服务端解码键盘事件
FNV_TO_CODE: dict = {fnv1a32(code): code for code in CODE_TO_VK}

# ---------- 输入事件 ----------

KIND_KEY = 0x01
KIND_MOUSE_MOVE = 0x02
KIND_MOUSE_BUTTON = 0x03
KIND_WHEEL = 0x04
KIND_GAMEPAD = 0x05
KIND_TOUCH = 0x06
KIND_TEXT = 0x07
KIND_CUSTOM = 0x7F

# 事件体格式(不含 kind/ts 公共头),小端
_BODY_FMT = {
    KIND_KEY: "<BIBB",                       # down u8, code u32, mods u8, repeat u8
    KIND_MOUSE_MOVE: "<ffhh",                # x f32, y f32, dx i16, dy i16
    KIND_MOUSE_BUTTON: "<BBB",               # button u8, down u8, clicks u8
    KIND_WHEEL: "<ffB",                      # dx f32, dy f32, ctrl u8
    KIND_GAMEPAD: "<BBIffffff",              # idx u8, dpad u8, buttons u32, 6×f32
    KIND_TOUCH: "<BBfff",                    # id u8, phase u8, x y pressure f32
    KIND_TEXT: "<H",                         # len u16 + utf8
    KIND_CUSTOM: "<BH",                      # plugin u8, len u16 + data
}

_HEADER = struct.Struct("<4sBIB")  # magic ver seq n  => 10 bytes


@dataclass
class InputEvent:
    kind: int
    ts_ms: int
    # 按 kind 使用
    data: Tuple = field(default=())

    @staticmethod
    def key(down: int, code: str, modifiers: int = 0, repeat: int = 0,
            ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_KEY, ts_ms, (down, fnv1a32(code), modifiers, repeat))

    @staticmethod
    def mouse_move(x: float, y: float, dx: int = 0, dy: int = 0,
                   ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_MOUSE_MOVE, ts_ms, (x, y, dx, dy))

    @staticmethod
    def mouse_button(button: int, down: int, clicks: int = 1,
                     ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_MOUSE_BUTTON, ts_ms, (button, down, clicks))

    @staticmethod
    def wheel(dx: float, dy: float, ctrl: int = 0, ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_WHEEL, ts_ms, (dx, dy, ctrl))

    @staticmethod
    def text(text: str, ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_TEXT, ts_ms, (text,))

    @staticmethod
    def gamepad(index: int, buttons: int, axes: Tuple[float, ...],
                dpad: int = 0, ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_GAMEPAD, ts_ms, (index, dpad, buttons, *axes[:6]))

    @staticmethod
    def custom(plugin_id: int, payload: bytes, ts_ms: int = 0) -> "InputEvent":
        return InputEvent(KIND_CUSTOM, ts_ms, (plugin_id, payload))


def _pack_event(event: InputEvent) -> bytes:
    head = struct.pack("<BQ", event.kind, event.ts_ms)
    if event.kind in (KIND_TEXT, KIND_CUSTOM):
        if event.kind == KIND_TEXT:
            data, = event.data
            if isinstance(data, str):
                data = data.encode("utf-8")
            return head + struct.pack(_BODY_FMT[event.kind], len(data)) + data
        plugin_id, data = event.data
        return head + struct.pack(_BODY_FMT[event.kind], plugin_id, len(data)) + data
    return head + struct.pack(_BODY_FMT[event.kind], *event.data)


def encode_input_frame(events: List[InputEvent], seq: int = 0) -> bytes:
    """打包输入帧:magic CRIN + ver + seq + n + events。"""
    n = min(len(events), 255)
    body = b"".join(_pack_event(e) for e in events[:n])
    seq = int(seq) & 0xFFFFFFFF
    return _HEADER.pack(MAGIC_INPUT, PROTOCOL_VERSION, seq, n) + body


def decode_input_frame(data: bytes) -> Tuple[int, List[InputEvent]]:
    """解析输入帧,返回 (seq, [InputEvent...])。非法数据抛 ValueError。"""
    if len(data) < 10 or data[:4] != MAGIC_INPUT:
        raise ValueError("bad magic")
    _, ver, seq, n = _HEADER.unpack_from(data, 0)
    if ver != PROTOCOL_VERSION:
        raise ValueError(f"unsupported version {ver}")
    events: List[InputEvent] = []
    off = 10
    for _ in range(n):
        if off + 9 > len(data):
            raise ValueError("truncated event head")
        kind, ts = struct.unpack_from("<BQ", data, off)
        off += 9
        if kind in (KIND_TEXT, KIND_CUSTOM):
            if off + 2 > len(data):
                raise ValueError("truncated length")
            if kind == KIND_TEXT:
                size, = struct.unpack_from(_BODY_FMT[kind], data, off)
                off += 2
            else:
                plugin_id = data[off]
                size, = struct.unpack_from("<H", data, off + 1)
                off += 3
            if off + size > len(data):
                raise ValueError("truncated payload")
            payload = data[off:off + size]
            off += size
            if kind == KIND_TEXT:
                events.append(InputEvent(kind, ts, (payload.decode("utf-8", "replace"),)))
            else:
                events.append(InputEvent(kind, ts, (plugin_id, payload)))
            continue
        fmt = _BODY_FMT.get(kind)
        if fmt is None:
            raise ValueError(f"unknown kind {kind:#x}")
        size = struct.calcsize(fmt)
        if off + size > len(data):
            raise ValueError("truncated event body")
        events.append(InputEvent(kind, ts, struct.unpack_from(fmt, data, off)))
        off += size
    return seq, events


# ---------- events 通道(S→C) ----------

EVT_CURSOR_POS = 0x01
EVT_QUALITY = 0x02
EVT_TOAST = 0x03
EVT_PONG = 0x04


def encode_event_frame(events: List[bytes], seq: int = 0) -> bytes:
    n = min(len(events), 255)
    return _HEADER.pack(MAGIC_EVENT, PROTOCOL_VERSION, int(seq) & 0xFFFFFFFF, n) + b"".join(
        events[:n])


def ev_pack(kind: int, body: bytes, ts_ms: int = 0) -> bytes:
    return struct.pack("<BQ", kind, ts_ms) + body


def ev_cursor_pos(x: float, y: float, ts_ms: int = 0) -> bytes:
    return ev_pack(EVT_CURSOR_POS, struct.pack("<ff", x, y), ts_ms)


def ev_quality(bitrate_kbps: int, fps: int, ts_ms: int = 0) -> bytes:
    return ev_pack(EVT_QUALITY, struct.pack("<IB", bitrate_kbps, fps), ts_ms)


def ev_toast(text: str, ts_ms: int = 0) -> bytes:
    b = text.encode("utf-8")
    return ev_pack(EVT_TOAST, struct.pack("<H", len(b)) + b, ts_ms)