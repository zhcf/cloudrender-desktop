"""以 winlogon 令牌 spawn SYSTEM 子进程(安全桌面抓帧/输入 worker)。

原理(tools 阶段已实测打通):

- winlogon.exe 天然运行于交互会话,其令牌 SessionId 即目标会话(免改会话);
- 获取 winlogon 进程令牌需 SeDebugPrivilege;CreateProcessWithTokenW 需要
  调用者具备 SeImpersonatePrivilege(管理员提升后即有),无需
  SeTcbPrivilege/SeAssignPrimaryTokenPrivilege;
- 子进程以 SYSTEM 运行于交互会话,lpDesktop 优先 WinSta0\\Winlogon
  (worker 出生即位于安全桌面:抓帧线程与主线程输入队列直接归属该桌面,
  单进程内联完成抓帧+输入注入);失败回退 winsta0\\default(输入降级为
  线程附加/窗口消息兜底)。

失败(非管理员等)返回 None,由上层降级为"锁屏只推静止帧"。

(副本在 server/cpp/shell/winlogon.py,修改须两处同步)
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger("cloudrender.winlogon")

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ALL_ACCESS = 0xF01FF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SE_PRIVILEGE_ENABLED = 0x00000002
SecurityImpersonation = 2
TokenPrimary = 1
TokenImpersonation = 2
TH32CS_SNAPPROCESS = 0x2
CREATE_NO_WINDOW = 0x08000000
MAX_PATH = 260


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * MAX_PATH)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


# ---- API 签名 ----
kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(PROCESSENTRY32W)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                 wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.POINTER(LUID)]
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
advapi32.DuplicateTokenEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.CreateProcessWithTokenW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
advapi32.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
    wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION)]


def enable_privilege(name: str) -> bool:
    htok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(htok)):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ok = advapi32.AdjustTokenPrivileges(htok, False, ctypes.byref(tp),
                                            0, None, None)
        return bool(ok) and ctypes.GetLastError() == 0
    finally:
        kernel32.CloseHandle(htok)


def find_winlogon_token(session_id: int):
    """返回交互会话 winlogon.exe 的进程令牌句柄(需 SeDebugPrivilege)。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return None
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(pe)
    token = None
    try:
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == "winlogon.exe":
                    sid = wintypes.DWORD()
                    if (kernel32.ProcessIdToSessionId(pe.th32ProcessID,
                                                      ctypes.byref(sid))
                            and sid.value == session_id):
                        h = kernel32.OpenProcess(
                            PROCESS_QUERY_LIMITED_INFORMATION, False,
                            pe.th32ProcessID)
                        if h:
                            htok = wintypes.HANDLE()
                            if advapi32.OpenProcessToken(
                                    h, TOKEN_DUPLICATE | TOKEN_QUERY,
                                    ctypes.byref(htok)):
                                token = htok
                            kernel32.CloseHandle(h)
                        break
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return token


def _try_spawn(hdup, api: str, buf, desktop: str):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    pi = PROCESS_INFORMATION()
    if api == "withtoken":
        ok = advapi32.CreateProcessWithTokenW(
            hdup, 0, None, buf, CREATE_NO_WINDOW, None, None,
            ctypes.byref(si), ctypes.byref(pi))
    else:
        ok = advapi32.CreateProcessAsUserW(
            hdup, None, buf, None, None, False, CREATE_NO_WINDOW, None, None,
            ctypes.byref(si), ctypes.byref(pi))
    return bool(ok), pi


def spawn_wdesktop_worker(port: int, token: str,
                          log_path: Optional[str] = None) -> Optional[int]:
    """以 winlogon 令牌(交互会话, SYSTEM)spawn 安全桌面 worker;返回 PID。

    需管理员运行(SeDebugPrivilege + SeImpersonatePrivilege);失败返回 None。
    """
    if sys.platform != "win32":
        return None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "wdesktop_worker.py")
    if not enable_privilege("SeDebugPrivilege"):
        logger.warning("SeDebugPrivilege 开启失败(服务端需以管理员运行),"
                       "无法启动锁屏画面 worker")
        return None
    enable_privilege("SeImpersonatePrivilege")

    active = kernel32.WTSGetActiveConsoleSessionId()
    htok = find_winlogon_token(active)
    if not htok:
        logger.warning("未找到会话 %d 的 winlogon 令牌,无法启动锁屏画面 worker",
                       active)
        return None
    try:
        cmd = ('"%s" "%s" --port %d --parent %d --token %s'
               % (sys.executable, script, port, os.getpid(), token))
        if log_path:
            cmd += ' --log "%s"' % log_path
        buf = ctypes.create_unicode_buffer(cmd)
        # 出生桌面优先 Winlogon(worker 单进程内联输入的前提),失败回退
        # default。每个桌面依次尝试三种令牌/API 组合(历史实测兼容顺序)。
        for desktop in ("WinSta0\\Winlogon", "winsta0\\default"):
            for tt, api in ((TokenImpersonation, "withtoken"),
                            (TokenPrimary, "withtoken"),
                            (TokenPrimary, "asuser")):
                hdup = wintypes.HANDLE()
                if not advapi32.DuplicateTokenEx(
                        htok, TOKEN_ALL_ACCESS, None, SecurityImpersonation,
                        tt, ctypes.byref(hdup)):
                    continue
                ok, pi = _try_spawn(hdup, api, buf, desktop)
                kernel32.CloseHandle(hdup)
                if ok:
                    kernel32.CloseHandle(pi.hThread)
                    kernel32.CloseHandle(pi.hProcess)
                    sid = wintypes.DWORD()
                    kernel32.ProcessIdToSessionId(pi.dwProcessId,
                                                  ctypes.byref(sid))
                    logger.info("锁屏画面 worker 已启动(pid=%d 会话=%d api=%s "
                                "桌面=%s)", pi.dwProcessId, sid.value, api,
                                desktop)
                    return int(pi.dwProcessId)
        logger.warning("锁屏画面 worker spawn 失败(两种桌面×三种方式均未成功)")
        return None
    finally:
        kernel32.CloseHandle(htok)