"""安全桌面(锁屏画面/输入)worker 的本地客户端。

worker 由服务端以 winlogon 令牌 spawn(SYSTEM, 交互会话;见 winlogon.py
与 wdesktop_worker.py),对外提供:

- 锁屏时抓取安全桌面帧(普通进程在锁屏期间 DXGI/GDI 都无法抓屏);
- 锁屏时注入输入(SendInput 到 Winlogon 桌面,浏览器可输入锁屏密码)。

传输:127.0.0.1 TCP 单连接请求-响应(约 8MB/帧,BGRA);任何失败即断开,
下次调用自动重连。所有公开函数线程安全(锁内串行)。

(副本在 server/cpp/shell/wdesktop.py,修改须两处同步)
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger("cloudrender.wdesktop")

WDESKTOP_PORT = int(os.environ.get("CLOUDRENDER_WDESKTOP_PORT", "45990"))

_TIMEOUT_CONNECT = 0.4
_TIMEOUT_IO = 2.0

_lock = threading.Lock()
_sock: Optional[socket.socket] = None
_token = ""
# 当前生效端口:默认 WDESKTOP_PORT,set_port 可覆盖(同一主机跑多个服务端
# 时各自 spawn 独立 worker,避免互相挤同一端口)
_port = WDESKTOP_PORT


def set_port(port: int) -> None:
    """覆盖 worker 端口(须与 spawn 侧一致);断开现有连接。"""
    global _port
    with _lock:
        _port = int(port)
        _close_locked()


def get_port() -> int:
    """当前 worker 端口。"""
    return _port


def set_token(token: str) -> None:
    """设置连接认证 token(服务端 spawn worker 时生成的一致值)。"""
    global _token
    with _lock:
        _token = token or ""
        _close_locked()


def _close_locked() -> None:
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None


def _connect_locked() -> Optional[socket.socket]:
    global _sock
    if _sock is not None:
        return _sock
    try:
        s = socket.create_connection(("127.0.0.1", _port),
                                     timeout=_TIMEOUT_CONNECT)
        s.settimeout(_TIMEOUT_IO)
        s.sendall(("AUTH %s\n" % _token).encode("utf-8"))
        resp = s.recv(3)
        if resp != b"OK\n":
            s.close()
            return None
        _sock = s
        return _sock
    except OSError:
        return None


def _recv_exact(s: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def ping() -> bool:
    """worker 是否在线(轻量探活)。"""
    with _lock:
        s = _connect_locked()
        if s is None:
            return False
        try:
            s.sendall(b"P")
            return _recv_exact(s, 1) == b"\x00"
        except OSError:
            _close_locked()
            return False


def _simple(payload: bytes) -> bool:
    with _lock:
        s = _connect_locked()
        if s is None:
            return False
        try:
            s.sendall(payload)
            resp = _recv_exact(s, 1)
            if resp is None:
                _close_locked()
                return False
            return resp == b"\x00"
        except OSError:
            _close_locked()
            return False


def send_key(vk: int, down: bool) -> bool:
    return _simple(b"K" + struct.pack("<IB", int(vk), 1 if down else 0))


def send_mouse_move(x: int, y: int) -> bool:
    return _simple(b"M" + struct.pack("<ii", int(x), int(y)))


def send_mouse_move_rel(dx: int, dy: int) -> bool:
    return _simple(b"R" + struct.pack("<ii", int(dx), int(dy)))


def send_mouse_button(button: int, down: bool) -> bool:
    return _simple(b"B" + struct.pack("<IB", int(button), 1 if down else 0))


def send_mouse_wheel(delta: int) -> bool:
    return _simple(b"W" + struct.pack("<i", int(delta)))


def _grab_raw():
    """请求 worker 抓一帧;返回 (w, h, bytes) 或 None。"""
    with _lock:
        s = _connect_locked()
        if s is None:
            return None
        try:
            s.sendall(b"F")
            head = _recv_exact(s, 9)   # 1B 状态 + 4B 宽 + 4B 高
            if head is None:
                _close_locked()
                return None
            if head[0] != 0:
                return None
            w, h = struct.unpack("<II", head[1:])
            if not w or not h or w > 16384 or h > 16384:
                _close_locked()
                return None
            data = _recv_exact(s, w * h * 4)
            if data is None:
                _close_locked()
                return None
            return w, h, data
        except OSError:
            _close_locked()
            return None


def grab_secure(region) -> Optional["np.ndarray"]:
    """抓一帧安全桌面并按 region 裁剪;失败返回 None(上层推静止帧)。

    region 为 mss 风格 dict(left/top/width/height);帧尺寸不小于 region 时
    裁剪,否则放弃(尺寸不符视为不可用)。
    """
    raw = _grab_raw()
    if raw is None:
        return None
    w, h, data = raw
    img = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
    left = int(region["left"])
    top = int(region["top"])
    rw = int(region["width"])
    rh = int(region["height"])
    if left >= 0 and top >= 0 and left + rw <= w and top + rh <= h:
        img = img[top:top + rh, left:left + rw]
        return np.ascontiguousarray(img)
    return None


def probe() -> bool:
    """自检:能否从 worker 取到一帧(忽略内容)。"""
    return _grab_raw() is not None