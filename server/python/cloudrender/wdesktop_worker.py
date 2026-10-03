"""[SYSTEM/交互会话] 安全桌面(锁屏)抓帧与输入 worker(单进程)。

由服务端(管理员)以 winlogon 令牌 spawn,本进程即为 SYSTEM、运行于交互会话,
出生桌面优先 WinSta0\\Winlogon(失败回退 winsta0\\default):

- 抓帧:锁屏时 DXGI/GDI 从普通进程都无法抓取安全桌面;本 worker 的线程
  SetThreadDesktop(Winlogon) 后 GDI BitBlt + GetDIBits 可正常抓到锁屏画面
  (密码输入界面),供浏览器端显示;
- 输入:向安全桌面注入输入受系统多重限制(SendInput 可能返回 err=5),
  本 worker 采用**多策略注入链**(命中即停,首成功写日志):
    H0 出生桌面直连:worker 出生于 Winlogon 时,主线程输入队列(启动时
       PeekMessage 创建)即归属该桌面,直接 SendInput 注入(单进程内联,
       原独立输入 helper 子进程已合并至此);
    S1 线程附加+SendInput:新线程 SetThreadDesktop(Winlogon, MAXIMUM_ALLOWED)
       + PeekMessage 建输入队列 + SendInput;
    S3 窗口消息:向锁屏窗口 PostMessage WM_KEYDOWN/WM_KEYUP(键盘兜底,
       由目标消息循环自行 TranslateMessage)。

通过 127.0.0.1 TCP 单连接为服务端提供能力(小端序):
  请求 = 1B 命令 [+ 参数];响应 = 1B 状态(0=OK,1=失败) [+ 数据]
    P                        -> ping
    F                        -> 抓一帧: status + w(4B) + h(4B) + BGRA(w*h*4)
    K vk(4B) down(1B)        -> 键盘(含抬起)
    M x(4B) y(4B)            -> 鼠标绝对移动(虚拟屏坐标)
    R dx(4B) dy(4B)          -> 鼠标相对移动
    B btn(4B) down(1B)       -> 鼠标按钮(0=左 1=中 2=右)
    W delta(4B, 有符号)      -> 滚轮
    Q                        -> 退出
连接首行须为 "AUTH <token>\\n"(防止本机其它进程滥用 SYSTEM 注入)。

开发/运维:在日志文件旁放置 "<日志路径>.reload" 文件,worker 会在 5s 内
主动退出(服务端 keeper 随后自动以新代码重新拉起,免去重启服务端)。

用法(通常由服务端自动 spawn,不手工运行):
  python wdesktop_worker.py --port 45990 --parent <server_pid> --token <hex>

(副本在 server/cpp/shell/wdesktop_worker.py,修改须两处同步)
"""
from __future__ import annotations

import argparse
import ctypes
import os
import select
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

DESKTOP_ACCESS = 0x02000000        # MAXIMUM_ALLOWED(由 ACL 授予 SYSTEM 最大权限)
MAXIMUM_ALLOWED = 0x02000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAPVK_VK_TO_VSC = 0
MAPVK_VK_TO_CHAR = 2
PM_NOREMOVE = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

_EXTENDED_VKS = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28,
                 0xA3, 0xA5, 0x5B, 0x5C, 0x90, 0x6F}

user32.OpenDesktopW.restype = ctypes.c_void_p
user32.OpenDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                ctypes.c_int, ctypes.c_ulong]
user32.CloseDesktop.argtypes = [ctypes.c_void_p]
user32.SetThreadDesktop.restype = ctypes.c_int
user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
user32.GetThreadDesktop.restype = ctypes.c_void_p
user32.GetThreadDesktop.argtypes = [ctypes.c_ulong]
user32.OpenInputDesktop.restype = ctypes.c_void_p
user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                    ctypes.c_ulong]
kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
user32.GetUserObjectInformationW.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong)]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.MapVirtualKeyW.restype = ctypes.c_uint
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.BitBlt.restype = ctypes.c_int
gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                         ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                            ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_uint]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.StretchBlt.restype = ctypes.c_int
gdi32.StretchBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_ulong]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                 ctypes.c_ulong]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_ulong,
                                          ctypes.POINTER(ctypes.c_ulong)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


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


user32.SendInput.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT),
                             ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.PostMessageW.restype = ctypes.c_int
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_ssize_t]
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_ssize_t)
user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_ssize_t]
user32.EnumWindows.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

_log_path = None
_parent_pid = 0

_wl_lock = threading.Lock()
_wl_handle = None

# ---- 输入链状态 -----------------------------------------------------------
_born_secure = False               # 出生于 Winlogon 安全桌面(spawn lpDesktop 决定)
_msg_hwnd = None                   # 锁屏窗口句柄(S3 窗口消息策略,缓存)
_h0_diag_done = False              # 诊断:H0 首次失败时记录桌面上下文(仅一次)

_fail_stat = {}                    # (策略, 事件类型) -> [次数, 上次日志时间]
_ok_logged = set()                 # 已记录过"首次成功"的 (策略, 事件类型)


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except OSError:
        pass
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def note_fail(strategy: str, kind: str, err: int) -> None:
    """注入失败聚合日志(3s 一条,避免锁屏高频输入刷屏)。"""
    ent = _fail_stat.get((strategy, kind))
    if ent is None:
        ent = [0, time.monotonic()]
        _fail_stat[(strategy, kind)] = ent
    ent[0] += 1
    now = time.monotonic()
    if now - ent[1] >= 3.0:
        log("输入注入失败[%s/%s]: 近段 %d 次 err=%d"
            % (strategy, kind, ent[0], err))
        ent[0] = 0
        ent[1] = now


def note_ok(strategy: str, kind: str) -> None:
    key = (strategy, kind)
    if key not in _ok_logged:
        _ok_logged.add(key)
        log("输入通道可用: %s(%s) 首次成功" % (strategy, kind))


def _reload_flag_path() -> str:
    base = _log_path or os.path.join(os.getcwd(), "_wdesktop_worker.log")
    return base + ".reload"


def check_reload() -> None:
    """重载标记:存在则主动退出,由服务端 keeper 重新拉起(便于热更新)。"""
    path = _reload_flag_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        log("检测到重载标记,worker 主动退出(keeper 将重新拉起)")
        os._exit(0)


def parent_alive() -> bool:
    if not _parent_pid:
        return True
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                             _parent_pid)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def winlogon_handle():
    """进程级缓存的 Winlogon 桌面句柄(MAXIMUM_ALLOWED);失败返回 None。"""
    global _wl_handle
    with _wl_lock:
        if _wl_handle:
            return _wl_handle
        h = user32.OpenDesktopW("Winlogon", 0, False, DESKTOP_ACCESS)
        if not h:
            return None
        _wl_handle = h
        return h


def reset_winlogon_handle() -> None:
    global _wl_handle
    with _wl_lock:
        if _wl_handle:
            user32.CloseDesktop(_wl_handle)
            _wl_handle = None


def desk_name(hdesk) -> str:
    if not hdesk:
        return "?"
    buf = ctypes.create_unicode_buffer(64)
    need = ctypes.c_ulong()
    if user32.GetUserObjectInformationW(ctypes.c_void_p(hdesk), 2, buf,
                                        ctypes.sizeof(buf),
                                        ctypes.byref(need)):
        return buf.value or "?"
    return "?"


def thread_desk_name() -> str:
    return desk_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))


def input_desk_name() -> str:
    """当前输入(活动)桌面名;OpenInputDesktop 失败返回错误描述。"""
    h = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not h:
        return "?err=%d" % ctypes.GetLastError()
    try:
        return desk_name(h)
    finally:
        user32.CloseDesktop(h)


# ---------------------------------------------------------------------------
# 抓帧(锁屏画面)
# ---------------------------------------------------------------------------

def grab_bgra():
    """抓取 Winlogon 桌面一帧;返回 (w, h, bytes) 或 None。

    与 tools 阶段实测一致:每次开新线程 SetThreadDesktop(Winlogon) 后
    BitBlt + GetDIBits(top-down 32bpp → BGRA)。
    """
    hdesk = winlogon_handle()
    if not hdesk:
        log("OpenDesktop(Winlogon) 失败 err=%d" % ctypes.GetLastError())
        return None
    res = {}

    def _run():
        if not user32.SetThreadDesktop(hdesk):
            res["err"] = "SetThreadDesktop err=%d" % ctypes.GetLastError()
            return
        w = int(user32.GetSystemMetrics(0))
        h = int(user32.GetSystemMetrics(1))
        hdc = user32.GetDC(None)
        if not hdc:
            res["err"] = "GetDC err=%d" % ctypes.GetLastError()
            return
        memdc = bmp = old = None
        try:
            memdc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            old = gdi32.SelectObject(memdc, bmp)
            if not gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020):
                res["err"] = "BitBlt err=%d" % ctypes.GetLastError()
                return
            gdi32.SelectObject(memdc, old)   # 必须先移出 DC 才能 GetDIBits
            old = None
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h                # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            lines = gdi32.GetDIBits(hdc, bmp, 0, h, buf,
                                    ctypes.byref(bmi), 0)
            if lines != h:
                res["err"] = "GetDIBits 行数 %d != %d" % (lines, h)
                return
            res["w"], res["h"], res["data"] = w, h, buf.raw
        finally:
            if old:
                gdi32.SelectObject(memdc, old)
            if bmp:
                gdi32.DeleteObject(bmp)
            if memdc:
                gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(20)
    if t.is_alive():
        log("抓帧线程超时(20s),放弃本帧")
        reset_winlogon_handle()
        return None
    if res.get("err"):
        log("抓帧失败: %s" % res["err"])
        reset_winlogon_handle()
        return None
    return res["w"], res["h"], res["data"]


# ---------------------------------------------------------------------------
# 锁屏相位探测(E1 实验数据):每 2s 采样输入桌面名/LockApp(LogonUI)进程/
# 前台窗口类,并从 input/Winlogon/Default 三个桌面各抓一张 240x135 缩略帧
# (亮度均值 + 环形存盘 _deskprobe/p%05d_<tag>.bin)。
# 目的:锁屏"黑屏"阶段(检测 false)是否有可抓取画面 → 决定帧桥修复方向;
# 仅在疑似锁屏相位记录详细日志(其余时间 60s 一条心跳),避免日志膨胀。
# ---------------------------------------------------------------------------

kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
kernel32.Process32FirstW.restype = ctypes.c_int
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.Process32NextW.restype = ctypes.c_int
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

_PROBE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_deskprobe")
_PROBE_W, _PROBE_H = 240, 135
_PROBE_KEEP = 600          # 环形保留文件数(600/3 张 ≈ 400s 采样窗口)
_probe_seq = 0
_probe_session = 0


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def _probe_procs() -> str:
    """枚举锁屏相关进程(LockApp/LogonUI),返回 'LogonUI.exe=1234,...' 或 '-'。"""
    out = []
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == (2 ** 64 - 1):
        return "?"
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() in ("lockapp.exe", "logonui.exe"):
                    out.append("%s=%d" % (pe.szExeFile, pe.th32ProcessID))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return ",".join(out) if out else "-"


def _probe_fg() -> str:
    """当前(探测线程所属桌面)前台窗口类名;取不到返回 '-'。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            buf = ctypes.create_unicode_buffer(128)
            if user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 128):
                return buf.value or "-"
    except OSError:
        pass
    return "-"


def _probe_grab(hdesk, seq: int, tag: str) -> dict:
    """从指定桌面抓 240x135 缩略帧;返回 {br, err};不抛出。"""
    res = {"br": -1.0, "err": ""}
    if not hdesk:
        res["err"] = "hdesk=0"
        return res

    def _run():
        if not user32.SetThreadDesktop(hdesk):
            res["err"] = "SetThreadDesktop err=%d" % ctypes.GetLastError()
            return
        sw = int(user32.GetSystemMetrics(0))
        sh = int(user32.GetSystemMetrics(1))
        hdc = user32.GetDC(None)
        if not hdc:
            res["err"] = "GetDC err=%d" % ctypes.GetLastError()
            return
        memdc = bmp = old = None
        try:
            memdc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, _PROBE_W, _PROBE_H)
            old = gdi32.SelectObject(memdc, bmp)
            gdi32.SetStretchBltMode(memdc, 4)     # HALFTONE
            if not gdi32.StretchBlt(memdc, 0, 0, _PROBE_W, _PROBE_H, hdc,
                                    0, 0, sw, sh, 0x00CC0020):   # SRCCOPY
                res["err"] = "StretchBlt err=%d" % ctypes.GetLastError()
                return
            gdi32.SelectObject(memdc, old)         # 移出 DC 才能 GetDIBits
            old = None
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = _PROBE_W
            bmi.biHeight = -_PROBE_H              # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(_PROBE_W * _PROBE_H * 4)
            lines = gdi32.GetDIBits(hdc, bmp, 0, _PROBE_H, buf,
                                    ctypes.byref(bmi), 0)
            if lines != _PROBE_H:
                res["err"] = "GetDIBits %d!=%d" % (lines, _PROBE_H)
                return
            data = buf.raw
            n = _PROBE_W * _PROBE_H
            tot = cnt = 0
            for i in range(0, n, 16):              # 步长采样亮度
                o = i * 4
                tot += data[o] + data[o + 1] + data[o + 2]
                cnt += 1
            res["br"] = tot / (3.0 * cnt)
            with open(os.path.join(_PROBE_DIR,
                                   "p%05d_%s.bin" % (seq, tag)), "wb") as f:
                f.write(data)
        finally:
            if old:
                gdi32.SelectObject(memdc, old)
            if bmp:
                gdi32.DeleteObject(bmp)
            if memdc:
                gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(10)
    if t.is_alive():
        res["err"] = "timeout"
    return res


def _probe_fmt(r: dict) -> str:
    return "err(%s)" % r["err"] if r.get("err") else "br=%.1f" % r["br"]


def _probe_prune() -> None:
    """环形清理:仅保留最近 _PROBE_KEEP 个缩略文件。"""
    try:
        files = sorted(f for f in os.listdir(_PROBE_DIR)
                       if f.endswith(".bin"))
        for name in files[:-_PROBE_KEEP]:
            os.remove(os.path.join(_PROBE_DIR, name))
    except OSError:
        pass


def desk_probe_loop() -> None:
    """锁屏相位采样循环(daemon;每 2s 一轮,任何异常都不允许退出)。"""
    global _probe_seq
    idle_beat = time.monotonic()
    detail_until = 0.0
    last_sig = None
    last_sig_t = 0.0
    try:
        os.makedirs(_PROBE_DIR, exist_ok=True)
    except OSError:
        return
    while True:
        try:
            time.sleep(2.0)
            _probe_seq += 1
            seq = _probe_seq
            in_name = input_desk_name()
            procs = _probe_procs()
            active = (not in_name.lower().startswith("default")
                      or procs != "-")
            if active:
                detail_until = time.monotonic() + 30.0
            if not active and time.monotonic() > detail_until:
                # 正常桌面:不抓帧,仅低频心跳(60s)
                now = time.monotonic()
                if now - idle_beat >= 60.0:
                    idle_beat = now
                    log("DP s=%d seq=%d idle in=%s" % (_probe_session, seq,
                                                       in_name))
                continue
            fg = _probe_fg()
            in_g = {"br": -1.0, "err": "hdesk=0"}
            h = user32.OpenInputDesktop(0, False, MAXIMUM_ALLOWED)
            if h:
                try:
                    in_g = _probe_grab(h, seq, "in")
                finally:
                    user32.CloseDesktop(h)
            h = user32.OpenDesktopW("Winlogon", 0, False, DESKTOP_ACCESS)
            wl_g = _probe_grab(h, seq, "wl") if h else {"br": -1.0,
                                                        "err": "hdesk=0"}
            if h:
                user32.CloseDesktop(h)
            h = user32.OpenDesktopW("Default", 0, False, DESKTOP_ACCESS)
            def_g = _probe_grab(h, seq, "def") if h else {"br": -1.0,
                                                          "err": "hdesk=0"}
            if h:
                user32.CloseDesktop(h)
            sig = (in_name, procs, round(in_g["br"]), round(wl_g["br"]),
                   round(def_g["br"]), bool(in_g["err"]),
                   bool(wl_g["err"]), bool(def_g["err"]))
            now = time.monotonic()
            if sig == last_sig and now - last_sig_t < 30.0:
                pass                # 与上轮完全相同:降频(30s 一条)
            else:
                last_sig = sig
                last_sig_t = now
                log("DP s=%d seq=%d in=%s procs=%s fg=%s | in:%s | wl:%s | def:%s"
                    % (_probe_session, seq, in_name, procs, fg,
                       _probe_fmt(in_g), _probe_fmt(wl_g), _probe_fmt(def_g)))
            _probe_prune()
        except Exception:       # noqa: BLE001 - 探测线程不允许退出
            continue


# ---------------------------------------------------------------------------
# INPUT 构造(与 inject.py 的 SendInput 实现保持一致)
# ---------------------------------------------------------------------------

def make_key(vk: int, down: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    if not down:
        flags |= KEYEVENTF_KEYUP
    inp.ki.dwFlags = flags
    return inp


def make_move(x: int, y: int) -> INPUT:
    vl = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vt = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    inp.mi.dx = int((x - vl) / max(1, vw) * 65535)
    inp.mi.dy = int((y - vt) / max(1, vh) * 65535)
    return inp


def make_move_rel(dx: int, dy: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_MOVE
    inp.mi.dx, inp.mi.dy = dx, dy
    return inp


def make_button(btn: int, down: bool) -> INPUT:
    flags = {0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
             1: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
             2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)}.get(btn)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    if flags:
        inp.mi.dwFlags = flags[0 if down else 1]
    return inp


def make_wheel(delta: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_WHEEL
    inp.mi.mouseData = delta & 0xFFFFFFFF
    return inp


# ---------------------------------------------------------------------------
# 输入注入策略 S1:线程附加 + SendInput(返回 (ok, err))
# ---------------------------------------------------------------------------

def send_inputs_on_winlogon(inputs):
    hdesk = winlogon_handle()
    if not hdesk:
        return False, ctypes.GetLastError() or -1
    res = {}

    def _run():
        if not user32.SetThreadDesktop(hdesk):
            res["err"] = ctypes.GetLastError()
            return
        # 在目标桌面创建线程输入队列(SendInput 依赖消息队列的桌面归属)
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
        if sent != n:
            res["err"] = ctypes.GetLastError()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        return False, -2
    return (not res.get("err")), res.get("err", 0)


# ---------------------------------------------------------------------------
# 输入注入策略 S3:向锁屏窗口投递按键消息(键盘兜底)
# ---------------------------------------------------------------------------

def _find_lock_hwnd():
    """在 Winlogon 桌面线程中探测锁屏前台窗口(枚举取可见最大者兜底)。"""
    global _msg_hwnd
    if _msg_hwnd:
        return _msg_hwnd
    hdesk = winlogon_handle()
    if not hdesk:
        return None
    res = {}

    def _run():
        if not user32.SetThreadDesktop(hdesk):
            return
        hwnd = user32.GetForegroundWindow()
        cls = ""
        if not hwnd:
            best = [0, None]

            def _cb(h, _l):
                try:
                    if not user32.IsWindowVisible(h):
                        return 1
                    rect = (ctypes.c_long * 4)()
                    user32.GetWindowRect(h, ctypes.byref(rect))
                    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
                    if area > best[0]:
                        best[0] = area
                        best[1] = h
                except Exception:
                    pass
                return 1

            user32.EnumWindows(WNDENUMPROC(_cb), 0)
            hwnd = best[1]
        if hwnd:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            cls = buf.value
        res["hwnd"], res["cls"] = hwnd, cls

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(5)
    if res.get("hwnd"):
        _msg_hwnd = res["hwnd"]
        log("锁屏窗口发现: hwnd=%#x class=%s" % (res["hwnd"], res.get("cls")))
    else:
        log("锁屏窗口未发现(GetForegroundWindow/EnumWindows 均为空)")
    return _msg_hwnd


def post_key_message(vk: int, down: bool) -> bool:
    hwnd = _find_lock_hwnd()
    if not hwnd:
        return False
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0x3A
    lparam = (scan << 16) | 1
    if not down:
        lparam |= 0xC0000000
    ok = user32.PostMessageW(hwnd, WM_KEYDOWN if down else WM_KEYUP,
                             vk, lparam)
    return bool(ok)


# ---------------------------------------------------------------------------
# 输入策略 H0:出生桌面直连(worker 出生于 Winlogon 时,主线程直接 SendInput;
# 主线程输入队列在 main() 启动时创建,归属 Winlogon 桌面)
# ---------------------------------------------------------------------------

def send_inputs_direct(inputs):
    """主线程直接 SendInput(不做 SetThreadDesktop,依赖出生桌面路径)。"""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent == n:
        return True, 0
    return False, ctypes.GetLastError()


# ---------------------------------------------------------------------------
# 输入分发:策略链(出生桌面直连 → 线程附加 → 窗口消息)
# ---------------------------------------------------------------------------

def build_inputs(c: int, payload: bytes):
    """解析命令参数 → (INPUT 列表, 事件类型名, 键盘 (vk, down) 或 None)。"""
    ch = chr(c)
    if ch == "K":
        vk, down = struct.unpack("<IB", payload)
        return [make_key(vk, bool(down))], "键盘", (vk, bool(down))
    if ch == "M":
        x, y = struct.unpack("<ii", payload)
        return [make_move(x, y)], "鼠标移动", None
    if ch == "R":
        dx, dy = struct.unpack("<ii", payload)
        return [make_move_rel(dx, dy)], "鼠标移动", None
    if ch == "B":
        btn, down = struct.unpack("<IB", payload)
        return [make_button(btn, bool(down))], "鼠标按键", None
    if ch == "W":
        (delta,) = struct.unpack("<i", payload)
        return [make_wheel(delta)], "滚轮", None
    return None, "?", None


def handle_input(c: int, payload: bytes) -> bool:
    global _h0_diag_done
    inputs, kind, key_info = build_inputs(c, payload)
    if inputs is None:
        return False
    # H0: 出生桌面直连(仅出生于 Winlogon 时可用)
    if _born_secure:
        ok, err = send_inputs_direct(inputs)
        if ok:
            note_ok("出生桌面直连", kind)
            return True
        note_fail("出生桌面直连", kind, err)
        if not _h0_diag_done:
            _h0_diag_done = True
            log("H0 首败诊断: 线程桌面=%s 输入桌面=%s err=%d"
                % (thread_desk_name(), input_desk_name(), err))
    # S1: 线程附加 + SendInput
    ok, err = send_inputs_on_winlogon(inputs)
    if ok:
        note_ok("线程附加+SendInput", kind)
        return True
    note_fail("线程附加+SendInput", kind, err)
    # S3: 窗口消息(仅键盘兜底)
    if key_info is not None:
        if post_key_message(key_info[0], key_info[1]):
            note_ok("窗口消息", kind)
            return True
        note_fail("窗口消息", kind, -3)
    return False


# ---------------------------------------------------------------------------
# TCP 服务(服务端连接)
# ---------------------------------------------------------------------------

def read_exact(conn, n: int):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def serve_client(conn, token: str) -> None:
    conn.settimeout(30.0)
    auth = b""
    while not auth.endswith(b"\n") and len(auth) < 128:
        chunk = conn.recv(1)
        if not chunk:
            return
        auth += chunk
    if auth != ("AUTH %s\n" % token).encode("utf-8"):
        try:
            conn.sendall(b"ERR\n")
        except OSError:
            pass
        log("认证失败,拒绝连接")
        return
    conn.sendall(b"OK\n")
    log("服务端已连接(认证通过)")
    f_served = 0                      # 本连接已供安全帧数(诊断用)

    while True:
        if not parent_alive():
            log("父进程已退出,worker 结束")
            return
        try:
            ready, _, _ = select.select([conn], [], [], 5.0)
        except OSError:
            return
        if not ready:
            check_reload()
            continue
        try:
            cmd = read_exact(conn, 1)
        except (socket.timeout, OSError):
            return
        if cmd is None:
            return
        c = cmd[0]
        try:
            if c == ord("P"):
                conn.sendall(b"\x00")
            elif c == ord("F"):
                result = grab_bgra()
                if result is None:
                    # 失败也回完整 9 字节头(客户端按固定长度读,避免超时等待)
                    conn.sendall(b"\x01" + b"\x00" * 8)
                else:
                    w, h, data = result
                    f_served += 1
                    if f_served == 1:
                        log("安全帧服务: 首帧 %dx%d bytes=%d"
                            % (w, h, len(data)))
                    elif f_served % 300 == 0:
                        log("安全帧服务: 已供帧 %d" % f_served)
                    conn.sendall(b"\x00" + struct.pack("<II", w, h) + data)
            elif c in (ord("K"), ord("M"), ord("R"), ord("B"), ord("W")):
                need = {ord("K"): 5, ord("M"): 8, ord("R"): 8,
                        ord("B"): 5, ord("W"): 4}[c]
                payload = read_exact(conn, need)
                if payload is None:
                    return
                ok = handle_input(c, payload)
                conn.sendall(b"\x00" if ok else b"\x01")
            elif c == ord("Q"):
                conn.sendall(b"\x00")
                return
            else:
                conn.sendall(b"\x01")
        except OSError:
            return


def main() -> None:
    global _log_path, _parent_pid, _born_secure, _probe_session
    parser = argparse.ArgumentParser(description="CloudRender 安全桌面 worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent", type=int, default=0,
                        help="父进程(服务端)PID,父进程退出后本进程自动结束")
    parser.add_argument("--token", default="", help="连接认证 token")
    parser.add_argument("--log", default="", help="日志文件路径(可空)")
    args = parser.parse_args()
    _log_path = args.log or None
    _parent_pid = args.parent

    try:
        # 物理像素坐标:保证 GetSystemMetrics 尺寸与服务端捕获 region 一致
        # (高 DPI 缩放下,非 DPI 感知进程会按逻辑分辨率返回,导致帧尺寸不匹配)
        user32.SetProcessDPIAware()
    except OSError:
        pass

    desk = thread_desk_name()
    _born_secure = "winlogon" in desk.lower()
    if _born_secure:
        # 主线程创建输入队列(归属 Winlogon 桌面;后续 H0 直发 SendInput 依赖)
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

    sid = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                  ctypes.byref(sid))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", args.port))
    except OSError as exc:
        log("端口 %d 绑定失败: %s(可能已有 worker 在运行)" % (args.port, exc))
        sys.exit(1)
    srv.listen(2)
    log("worker 启动: pid=%d 会话=%d port=%d parent=%d 出生桌面=%s"
        % (os.getpid(), sid.value, args.port, _parent_pid, desk))
    if _born_secure:
        log("输入主通道: 出生桌面直连(单进程内联 SendInput)可用")
    else:
        log("警告: 未出生于 Winlogon 桌面,输入仅剩降级链(线程附加/窗口消息),"
            "请检查服务端 spawn 的 lpDesktop 是否生效")

    _probe_session = sid.value
    threading.Thread(target=desk_probe_loop, daemon=True,
                     name="desk-probe").start()

    while parent_alive():
        srv.settimeout(5.0)
        try:
            conn, _addr = srv.accept()
        except socket.timeout:
            check_reload()
            continue
        except OSError:
            break
        try:
            serve_client(conn, args.token)
        except Exception as exc:   # noqa: BLE001 - worker 不允许退出
            log("客户端处理异常: %r" % (exc,))
        finally:
            try:
                conn.close()
            except OSError:
                pass
    log("worker 退出(父进程不存在)")
    try:
        srv.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()