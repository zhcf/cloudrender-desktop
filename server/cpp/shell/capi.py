"""cloudrender_session.dll(会话核心 C ABI)的 ctypes 绑定。

DLL 内含 libwebrtc 服务端会话 + nativecore 直连抓屏/注入;信令不跨线程
回调 Python:内部排队,由 cr_session_poll_signal 轮询拉取。

加载顺序:环境变量 CLOUDRENDER_SESSION_DLL(文件路径或目录) →
PyInstaller 冻结模式(exe 打包资源目录,三件套随包分发) →
仓库构建产物 server/cpp/sdk/build/{Release,Debug}/cloudrender_session.dll。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cloudrender.capi")

DLL_ENV = "CLOUDRENDER_SESSION_DLL"
DLL_NAME = "cloudrender_session.dll"

# 返回码(与 cr_session_api.h 对齐)
OK = 0
E_ARG = 1
E_STATE = 2
E_FAIL = 3

# add_dll_directory 句柄:必须保持引用,否则被 GC 后目录立即失效
_dll_dir_handles: list = []
_lib: Optional[ctypes.CDLL] = None
_lib_lock = threading.Lock()
# libwebrtc/nativecore 初始化非重入:create/destroy 串行化(桌面抓屏
# 同一显示器同时只允许一个 Duplication 实例,多会话本就会失败)
_session_lock = threading.Lock()


def _candidate_paths() -> list[Path]:
    """收集 DLL 候选路径(按优先级)。"""
    here = Path(__file__).resolve()
    repo = here.parents[3]  # cloudrender/
    out: list[Path] = []
    env = os.environ.get(DLL_ENV)
    if env:
        p = Path(env)
        out.append(p if p.suffix.lower() == ".dll" else p / DLL_NAME)
    if getattr(sys, "frozen", False):
        # 冻结模式:三件套随 exe 打包在资源目录根
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        out.append(meipass / DLL_NAME)
    for cfg in ("Release", "Debug"):
        out.append(repo / "server" / "cpp" / "sdk" / "build" / cfg / DLL_NAME)
    return out


def load_dll(path: Optional[str] = None) -> ctypes.CDLL:
    """加载并绑定 DLL(进程级单例)。"""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        if sys.platform != "win32":
            raise RuntimeError("cloudrender_session.dll supports Windows only")
        dll_path: Optional[Path] = None
        if path:
            p = Path(path)
            dll_path = p if p.suffix.lower() == ".dll" else p / DLL_NAME
        else:
            for cand in _candidate_paths():
                if cand.exists():
                    dll_path = cand
                    break
        if dll_path is None or not dll_path.exists():
            tried = "\n  ".join(str(p) for p in _candidate_paths())
            raise FileNotFoundError(
                f"{DLL_NAME} not found (set env var {DLL_ENV} to override). Tried:\n  {tried}")
        # 依赖(libwebrtc.dll/nativecore.dll)与本 DLL 同目录
        _dll_dir_handles.append(os.add_dll_directory(str(dll_path.parent)))
        lib = ctypes.CDLL(str(dll_path))
        _bind(lib)
        _lib = lib
        logger.info("已加载会话核心 %s", dll_path)
        return lib


def _bind(lib: ctypes.CDLL) -> None:
    lib.cr_session_create.restype = ctypes.c_void_p
    lib.cr_session_create.argtypes = [ctypes.c_char_p]
    lib.cr_session_destroy.restype = None
    lib.cr_session_destroy.argtypes = [ctypes.c_void_p]
    lib.cr_session_create_offer.restype = ctypes.c_void_p
    lib.cr_session_create_offer.argtypes = [ctypes.c_void_p]
    lib.cr_session_handle_signal.restype = ctypes.c_int
    lib.cr_session_handle_signal.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                             ctypes.c_char_p]
    lib.cr_session_poll_signal.restype = ctypes.c_void_p
    lib.cr_session_poll_signal.argtypes = [ctypes.c_void_p]
    lib.cr_session_connected.restype = ctypes.c_int
    lib.cr_session_connected.argtypes = [ctypes.c_void_p]
    lib.cr_session_get_source_size.restype = ctypes.c_int
    lib.cr_session_get_source_size.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.cr_session_get_stats.restype = ctypes.c_void_p
    lib.cr_session_get_stats.argtypes = [ctypes.c_void_p]
    lib.cr_session_set_bitrate.restype = ctypes.c_int
    lib.cr_session_set_bitrate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.cr_session_set_secure_mode.restype = ctypes.c_int
    lib.cr_session_set_secure_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.cr_session_push_secure_frame.restype = ctypes.c_int
    lib.cr_session_push_secure_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
    ]
    lib.cr_session_send_toast.restype = ctypes.c_int
    lib.cr_session_send_toast.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.cr_session_close.restype = None
    lib.cr_session_close.argtypes = [ctypes.c_void_p]
    lib.cr_session_free.restype = None
    lib.cr_session_free.argtypes = [ctypes.c_void_p]


def _take_json(lib: ctypes.CDLL, ptr: int) -> dict:
    """取走 DLL 返回的堆字符串并解析 JSON。"""
    text = ctypes.string_at(ptr).decode("utf-8", "replace")
    lib.cr_session_free(ctypes.c_void_p(ptr))
    return json.loads(text)


def _dumps(obj) -> str:
    """紧凑 JSON 序列化:与浏览器 JSON.stringify 字节风格一致,供 DLL 解析。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class CrSession:
    """单个会话句柄的薄封装(线程安全:允许在 to_thread 中调用)。"""

    def __init__(self, lib: ctypes.CDLL, config: dict):
        self._lib = lib
        self._handle: Optional[int] = None
        # 紧凑 JSON(与浏览器 JSON.stringify 一致;C++ 侧 JSON 提取器按紧凑
        # 格式解析,json.dumps 默认的 ": " 空格会让提取失败)
        cfg = _dumps(config).encode("utf-8")
        with _session_lock:
            handle = lib.cr_session_create(cfg)
        if not handle:
            raise RuntimeError("cr_session_create failed (init or capture start failed)")
        self._handle = handle

    # ---------------- 查询 ----------------
    @property
    def connected(self) -> bool:
        if not self._handle:
            return False
        return bool(self._lib.cr_session_connected(self._handle))

    def source_size(self) -> tuple[int, int]:
        if not self._handle:
            return (0, 0)
        w, h = ctypes.c_int(), ctypes.c_int()
        rc = self._lib.cr_session_get_source_size(self._handle,
                                                  ctypes.byref(w), ctypes.byref(h))
        if rc != OK or w.value <= 0 or h.value <= 0:
            return (0, 0)
        return (w.value, h.value)

    def get_stats(self) -> Optional[dict]:
        """发送侧累计统计快照(bytes_sent/frames_pushed);查询失败返回 None。"""
        if not self._handle:
            return None
        ptr = self._lib.cr_session_get_stats(self._handle)
        if not ptr:
            return None
        return _take_json(self._lib, ptr)

    def set_bitrate(self, bitrate_kbps: int) -> bool:
        """运行期固定目标码率(kbps,min=max);失败返回 False。幂等可重申。"""
        if not self._handle or bitrate_kbps <= 0:
            return False
        return self._lib.cr_session_set_bitrate(
            self._handle, bitrate_kbps * 1000) == OK

    def set_secure_mode(self, on: bool) -> bool:
        """切换安全桌面(锁屏)模式:帧泵改推 push_secure_frame 注入帧,
        输入帧经 poll_signal 的 secure_input 旁路;失败返回 False。幂等。"""
        if not self._handle:
            return False
        return self._lib.cr_session_set_secure_mode(
            self._handle, 1 if on else 0) == OK

    def push_secure_frame(self, bgra: bytes, width: int, height: int,
                          stride: int = 0, pts_us: int = 0) -> bool:
        """注入一帧安全桌面图像(BGRA32,stride=0 紧凑 width*4)。
        DLL 侧立即拷贝数据,返回后即可释放;失败返回 False。"""
        if not self._handle or not bgra or width <= 0 or height <= 0:
            return False
        return self._lib.cr_session_push_secure_frame(
            self._handle, bgra, width, height, stride, pts_us) == OK

    def send_toast(self, text: str) -> bool:
        """经 events 数据通道发送一条 toast 提示;未连接/通道未就绪返回 False。"""
        if not self._handle:
            return False
        return self._lib.cr_session_send_toast(
            self._handle, text.encode("utf-8")) == OK

    # ---------------- 信令 ----------------
    def create_offer(self) -> str:
        if not self._handle:
            raise RuntimeError("session is closed")
        ptr = self._lib.cr_session_create_offer(self._handle)
        if not ptr:
            raise RuntimeError("cr_session_create_offer failed")
        sdp = ctypes.string_at(ptr).decode("utf-8", "replace")
        self._lib.cr_session_free(ctypes.c_void_p(ptr))
        return sdp

    def handle_signal(self, msg_type: str, payload: dict) -> int:
        if not self._handle:
            return E_STATE
        body = _dumps(payload or {}).encode("utf-8")
        return self._lib.cr_session_handle_signal(
            self._handle, msg_type.encode("utf-8"), body)

    def poll_signal(self) -> Optional[dict]:
        """拉取一条待发信令(非阻塞)。无数据返回 None。"""
        if not self._handle:
            return None
        ptr = self._lib.cr_session_poll_signal(self._handle)
        if not ptr:
            return None
        return _take_json(self._lib, ptr)

    # ---------------- 生命周期 ----------------
    def close(self) -> None:
        """关闭并销毁(幂等;可在任意线程调用)。"""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        self._lib.cr_session_close(handle)
        with _session_lock:
            self._lib.cr_session_destroy(handle)