"""C++ 会话壳(cpp_server)的锁屏/安全桌面辅助工具。

与 aiortc 版(session.py/capture.py)功能对齐但独立实现(该版保持不动):

- lock_workstation:锁定当前交互式工作站(客户端"锁屏"按钮);
- secure_desktop_active:安全桌面检测(锁屏/UAC;OpenInputDesktop +
  LogonUI 探测,0.5s 缓存);
- foreground_blocks_injection:前台窗口完整性级别检测(UIPI 提示用);
- send_text_secure:文本 → 安全桌面按键序列(经 wdesktop worker 注入,
  逐字符 VkKeyScanW 含 Shift 处理)。

安全桌面帧抓取/键鼠注入直接复用 wdesktop.py(worker 客户端)。
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Optional

# 依赖模块 wdesktop 平铺于本目录,直接 import(自包含,无 server/python 依赖)
import wdesktop

logger = logging.getLogger("cloudrender.secure_ops")

# 完整性级别 RID(Medium=0x2000/High=0x3000/System=0x4000);UIPI 规则:
# 注入进程的 IL 低于目标窗口进程时,系统静默丢弃该次注入(无任何报错)。
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25


def _integrity_rid(handle) -> int:
    """进程句柄 → 完整性级别 RID(最低有效子授权);查询失败返回 0。"""
    adv = ctypes.windll.advapi32
    adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)
    adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]

    class _TokenMandatoryLabel(ctypes.Structure):
        _fields_ = [("Label", _SidAndAttributes)]

    tok = ctypes.c_void_p()
    if not adv.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(tok)):
        return 0
    try:
        length = ctypes.c_ulong()
        adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, None, 0,
                                ctypes.byref(length))
        buf = ctypes.create_string_buffer(length.value)
        if not adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, buf,
                                       length.value, ctypes.byref(length)):
            return 0
        label = ctypes.cast(buf, ctypes.POINTER(_TokenMandatoryLabel)).contents
        sid = ctypes.c_void_p(label.Label.Sid)
        cnt = adv.GetSidSubAuthorityCount(sid)
        return int(adv.GetSidSubAuthority(sid, cnt.contents.value - 1).contents.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(tok)


def foreground_blocks_injection() -> Optional[bool]:
    """前台窗口是否属于更高完整性的进程(此时 UIPI 丢本进程的注入输入)。

    "打开任务管理器后点不动"正是此场景:任务管理器以 High IL 运行,
    非管理员服务端(Medium IL)向它注入被系统静默阻止。
    非 Windows 或无法判定时返回 None。
    """
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    _pq = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    h = kernel32.OpenProcess(_pq, 0, pid.value)
    if not h:
        return None
    try:
        target = _integrity_rid(h)
    finally:
        kernel32.CloseHandle(h)
    me = kernel32.OpenProcess(_pq, 0, kernel32.GetCurrentProcessId())
    if not me:
        return None
    try:
        mine = _integrity_rid(me)
    finally:
        kernel32.CloseHandle(me)
    if not target or not mine:
        return None
    return target > mine


def lock_workstation() -> bool:
    """锁定当前交互式工作站(等效 Win+L);非交互会话下调用失败,返回 False。"""
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:
        logger.debug("LockWorkStation 调用失败", exc_info=True)
        return False


def send_text_secure(text: str) -> None:
    """文本 → 安全桌面按键序列(逐字符 VkKeyScanW,含 Shift 处理)。"""
    user32 = ctypes.windll.user32
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    vk_shift = 0x10
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\n":
            vk, need_shift = 0x0D, False
        elif ch == "\t":
            vk, need_shift = 0x09, False
        else:
            r = user32.VkKeyScanW(ch)
            if r == -1:
                continue
            vk, need_shift = r & 0xFF, bool((r >> 8) & 0x01)
        if need_shift:
            wdesktop.send_key(vk_shift, True)
        wdesktop.send_key(vk, True)
        wdesktop.send_key(vk, False)
        if need_shift:
            wdesktop.send_key(vk_shift, False)


# ---------------------------------------------------------------------------
# 安全桌面检测
#
# 锁屏/UAC 时输入桌面从 "Default" 切换为 "Winlogon"(安全桌面);普通进程
# 此时抓屏(DXGI/GDI)被系统拒绝,必须走 SYSTEM worker(见 wdesktop.py)。
# 检测用于:会话守护循环切换帧/输入通道 + 客户端 toast 提示。
# ---------------------------------------------------------------------------

_secure_cache = {"ts": 0.0, "active": False}
_SECURE_CHECK_TTL = 0.5  # 秒;守护循环每轮调用,缓存摊薄系统调用开销


def _input_desktop_name() -> Optional[str]:
    """当前输入桌面(接收键鼠的桌面)名称;查询失败返回 None。

    未锁屏为 "Default";锁屏/UAC 安全桌面为 "Winlogon" 等(已实测)。
    """
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                            ctypes.c_ulong]
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        user32.GetUserObjectInformationW.restype = ctypes.c_int
        user32.GetUserObjectInformationW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong)]
        h = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(64)
            need = ctypes.c_ulong()
            if not user32.GetUserObjectInformationW(
                    ctypes.c_void_p(h), 2, buf, ctypes.sizeof(buf),
                    ctypes.byref(need)):
                return None
            return buf.value or None
        finally:
            user32.CloseDesktop(ctypes.c_void_p(h))
    except Exception:
        return None


def _logonui_present() -> bool:
    """活动控制台会话中是否存在 LogonUI.exe(锁屏标志进程);失败返回 False。"""
    try:
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong,
                                                 ctypes.c_ulong]
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.ProcessIdToSessionId.argtypes = [
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        k32.WTSGetActiveConsoleSessionId.restype = ctypes.c_ulong

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]

        active = k32.WTSGetActiveConsoleSessionId()
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return False
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(pe)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                if pe.szExeFile.lower() == "logonui.exe":
                    sid = wintypes.DWORD()
                    if (k32.ProcessIdToSessionId(pe.th32ProcessID,
                                                 ctypes.byref(sid))
                            and sid.value == active):
                        return True
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return False


def secure_desktop_active() -> bool:
    """当前是否处于安全桌面(锁屏/密码输入/UAC 提升确认)。

    主路:OpenInputDesktop 读输入桌面名(非 "Default" 即安全桌面);
    备路(主路查询失败):探测活动控制台会话中的 LogonUI.exe。缓存 0.5s。
    """
    now = time.monotonic()
    if now - _secure_cache["ts"] < _SECURE_CHECK_TTL:
        return bool(_secure_cache["active"])
    name = _input_desktop_name()
    active = (name.lower() != "default") if name else _logonui_present()
    _secure_cache["ts"] = now
    _secure_cache["active"] = active
    return active