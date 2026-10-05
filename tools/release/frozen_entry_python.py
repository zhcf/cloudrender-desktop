"""PyInstaller 冻结入口(Python 版服务端,8080)。

分发:
- 默认:启动 cloudrender.server(aiortc 版服务端);
- --wdesktop-worker:以安全桌面 worker 模式运行(winlogon 令牌 spawn 的
  SYSTEM 子进程入口,替代源码模式下的 wdesktop_worker.py 脚本)。

源码运行不受影响(入口仍为 python -m cloudrender.server)。
"""
from __future__ import annotations

import sys


def _start_parent_watchdog() -> None:
    """监视父进程(cmd.exe):父退出(关窗/被强杀)后本进程立即退出。

    包内 start_server.cmd 保持 cmd 前台运行本 exe;关闭该窗口或在任务
    管理器中结束 cmd 时,服务端随之一并结束,不再残留占用端口。
    仅当父进程映像为 cmd.exe 时启用(其他宿主由关窗语义兜底)。
    """
    try:
        import ctypes
        import os
        import threading

        k32 = ctypes.windll.kernel32
        ppid = os.getppid()
        h_wait = k32.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
        if not h_wait:
            return
        h_query = k32.OpenProcess(0x1000, False, ppid)  # QUERY_LIMITED_INFORMATION
        is_cmd = False
        if h_query:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_uint(512)
            if k32.QueryFullProcessImageNameW(h_query, 0, buf, ctypes.byref(size)):
                is_cmd = buf.value.lower().endswith("\\cmd.exe")
            k32.CloseHandle(h_query)
        if not is_cmd:
            k32.CloseHandle(h_wait)
            return

        def _wait() -> None:
            k32.WaitForSingleObject(h_wait, 0xFFFFFFFF)
            os._exit(1)

        threading.Thread(target=_wait, daemon=True).start()
    except Exception:
        pass


def _run() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--wdesktop-worker":
        # 该标记仅用于本 exe 的模式分发,worker 自身的 argparse 不应看到它
        sys.argv = [sys.argv[0]] + argv[1:]
        from cloudrender.wdesktop_worker import main as worker_main
        worker_main()
    else:
        _start_parent_watchdog()
        from cloudrender.server import main as server_main
        server_main()


if __name__ == "__main__":
    _run()