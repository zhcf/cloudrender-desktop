#!/usr/bin/env python3
"""cloudrender_session.dll C ABI 冒烟(真实抓屏 + offer + 信令轮询)。

前置:cmake --build server/cpp/sdk/build --config Release --target cr_session_dll
用法:python session_capi_smoke.py [--fps N] [--monitor N] [--window HWND]
通过输出:SESSION_CAPI_SMOKE_OK(退出码 0)。

退出码:2=DLL 缺失;3=create 失败;4=捕获尺寸未就绪;5=create_offer 失败;
        6=SDP 缺少 m=video/m=application;7=5s 内无任何信令(ICE 未排队)。
"""
import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # server/cpp/sdk
DLL_DIR = ROOT / "build" / "Release"
DLL_PATH = DLL_DIR / "cloudrender_session.dll"


def bind(lib: ctypes.CDLL) -> None:
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
    lib.cr_session_close.restype = None
    lib.cr_session_close.argtypes = [ctypes.c_void_p]
    lib.cr_session_free.restype = None
    lib.cr_session_free.argtypes = [ctypes.c_void_p]


def take_string(lib: ctypes.CDLL, ptr: int) -> str:
    text = ctypes.string_at(ptr).decode("utf-8", "replace")
    lib.cr_session_free(ctypes.c_void_p(ptr))
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--monitor", type=int, default=-1, help="-1=主显示器")
    parser.add_argument("--window", type=lambda v: int(v, 0), default=0,
                        help="窗口句柄(如 0x1A2B3C),非 0 时按窗口捕获")
    args = parser.parse_args()

    if not DLL_PATH.exists():
        print(f"FAIL dll not found: {DLL_PATH}")
        return 2
    # libwebrtc.dll / nativecore.dll 与本 DLL 同目录。
    # 注意:add_dll_directory 的返回值必须保持引用,否则被 GC 后目录立即失效。
    _dll_dir = os.add_dll_directory(str(DLL_DIR))
    lib = ctypes.CDLL(str(DLL_PATH))
    bind(lib)
    print(f"[ok] loaded {DLL_PATH.name}")

    cfg = {"monitor": args.monitor, "fps": args.fps}
    if args.window:
        cfg["window_hwnd"] = args.window
    print(f"[cfg] {json.dumps(cfg)}")

    session = lib.cr_session_create(json.dumps(cfg).encode())
    if not session:
        print("FAIL cr_session_create")
        return 3
    print("[ok] session created(nativecore 抓屏已启动)")

    width, height = ctypes.c_int(), ctypes.c_int()
    rc = lib.cr_session_get_source_size(session, ctypes.byref(width),
                                        ctypes.byref(height))
    if rc != 0 or width.value <= 0 or height.value <= 0:
        print(f"FAIL source size rc={rc} {width.value}x{height.value}")
        lib.cr_session_destroy(session)
        return 4
    print(f"[ok] source size {width.value}x{height.value}")

    sdp_ptr = lib.cr_session_create_offer(session)
    if not sdp_ptr:
        print("FAIL cr_session_create_offer")
        lib.cr_session_destroy(session)
        return 5
    sdp = take_string(lib, sdp_ptr)
    has_video = "m=video" in sdp
    has_app = "m=application" in sdp
    print(f"[ok] offer {len(sdp)}B video={int(has_video)} app={int(has_app)}")
    if not (has_video and has_app):
        lib.cr_session_destroy(session)
        return 6

    # 本地描述已设置 → ICE gathering 开始,host 候选应在秒级内进入队列
    seen = []
    deadline = time.time() + 5.0
    while time.time() < deadline and not seen:
        ptr = lib.cr_session_poll_signal(session)
        if ptr:
            item = json.loads(take_string(lib, ptr))
            seen.append(item.get("type"))
            payload = item.get("payload")
            print(f"[sig] type={item.get('type')} payload={str(payload)[:100]}")
        else:
            time.sleep(0.05)
    if not seen:
        print("FAIL no signal within 5s")
        lib.cr_session_destroy(session)
        return 7
    print(f"[ok] signals: {seen}")

    print(f"[ok] connected={lib.cr_session_connected(session)} (无客户端,预期 0)")
    lib.cr_session_close(session)
    lib.cr_session_close(session)  # 幂等验证
    lib.cr_session_destroy(session)
    print("SESSION_CAPI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())