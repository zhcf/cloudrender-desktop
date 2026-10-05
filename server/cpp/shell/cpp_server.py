"""CloudRenderServer(C++ 会话核心版):aiohttp WebSocket 信令壳。

与 server.py(aiortc 版) 协议完全一致:connect → connected → ready → offer →
answer/ice 循环;媒体会话/抓屏/注入由 cloudrender_session.dll(C++ 会话核心)
承担,本进程只做信令桥接与会话治理。现有的 aiortc 实现保持不动,双轨并存。

用法(在 server/cpp/shell 目录运行):

    python cpp_server.py --port 8081

依赖模块(protocol/wdesktop/winlogon/webui 与 worker 脚本
wdesktop_worker.py)已随信令壳(cpp_server/cpp_session/capi/secure_ops)
平铺于本目录,直接 import——完全自包含,无 server/python 依赖(修改
任一共享逻辑须与 cloudrender 包内原件两处同步)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
import uuid
from typing import Optional

from aiohttp import WSMsgType, web

# 依赖模块(protocol/wdesktop/winlogon/webui)与信令壳(cpp_server/cpp_session/
# capi/secure_ops)全部平铺于本目录,直接 import(自包含,无 server/python 依赖)
from cpp_session import CppPeerSession
import protocol
import webui
import wdesktop
from winlogon import spawn_wdesktop_worker

logger = logging.getLogger("cloudrender.cpp_server")


class CppCloudRenderServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080,
                 token: Optional[str] = None, max_sessions: int = 1,
                 fps: int = 30, bitrate_kbps: int = 10000,
                 monitor: int = -1, window_hwnd: int = 0,
                 ice_servers: Optional[list] = None,
                 dll_path: Optional[str] = None,
                 wdesktop_port: int = 45995):
        self.host = host
        self.port = port
        self.token = token
        # nativecore 抓屏同一显示器同时只允许一个 Duplication 实例:
        # 多会话并发会抓屏失败,故默认 1(需要多会话时先做共享捕获)
        self.max_sessions = max_sessions
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self.monitor = monitor
        self.window_hwnd = window_hwnd
        self.ice_servers = ice_servers
        self.dll_path = dll_path
        # 安全桌面(锁屏)worker:默认 45995 与 aiortc 版隔离(aiortc 版
        # worker 用 45990 主端口;本版用 45995;单进程后不再占 +1 中继口)
        self.wdesktop_port = wdesktop_port
        self._wdesktop_token = ""
        self._wdesktop_spawn_ts = 0.0
        self._wdesktop_keeper_task: Optional[asyncio.Task] = None
        self._sessions: dict[str, CppPeerSession] = {}
        self._session_clients: dict[str, str] = {}   # session_id -> 客户端 IP
        self._session_ws: dict[str, web.WebSocketResponse] = {}

    # ------------------------------------------------------------------
    def build_app(self) -> web.Application:
        app = web.Application()
        app["cloudrender_cpp_server"] = self
        app.router.add_get("/ws", self._ws_handler)
        # 内置 Web UI:页面挂在 /(与 /ws 同端口);web 目录缺失时仅信令可用
        webui.install_web_routes(app)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    def run(self, **run_kwargs) -> None:
        """阻塞式运行(内部启动 aiohttp)。"""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        web.run_app(self.build_app(), host=self.host, port=self.port, **run_kwargs)

    # ------------------------------------------------------------------
    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        # 认证
        if self.token and request.query.get("token") != self.token:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_UNAUTHORIZED, message="invalid token", fatal=True))
            await ws.close(code=4401)
            return ws

        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        # 等待 connect(超时 10s)
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except asyncio.TimeoutError:
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_BAD_PARAM, message="connect timed out", fatal=True))
            await ws.close()
            return ws
        if first.get("type") != "connect":
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_BAD_PARAM, message="first message must be 'connect'", fatal=True))
            await ws.close()
            return ws
        client_info = (first.get("payload") or {}).get("client_info") or {}

        session_id = uuid.uuid4().hex[:12]
        session: Optional[CppPeerSession] = None
        remote = request.remote or ""
        # 同客户端重复连接治理(与 aiortc 版一致):浏览器重连风暴会产生
        # 多条并存会话,各自抓屏互挤;新连接到来时踢除同 IP 旧会话。
        # 注意:踢除需等完成后再做上限检查——否则 max_sessions=1 时
        # 同 IP 重连会被旧会话占位误判为"已达上限"(aiortc 版已同步
        # 改 1 并采用同样顺序)。
        await self._kick_duplicate_sessions(remote)
        if len(self._sessions) >= self.max_sessions:
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_MAX_SESSIONS, message="session limit reached", fatal=True))
            await ws.close(code=4402)
            return ws

        try:
            config: dict = {
                "monitor": self.monitor,
                "fps": self.fps,
                "bitrate_kbps": self.bitrate_kbps,
                # 默认局域网直连(host 候选即可连通,与 aiortc 版一致);
                # 需要 NAT 穿越时经 --ice 显式传入
                "ice_servers": self.ice_servers or [],
            }
            if self.window_hwnd:
                config["window_hwnd"] = self.window_hwnd

            session = CppPeerSession(
                config=config,
                on_signal=self._make_sender(ws),
                on_closed=self._make_closed_notifier(ws),
                dll_path=self.dll_path,
            )
            self._sessions[session_id] = session
            self._session_clients[session_id] = remote
            self._session_ws[session_id] = ws

            # 建会话(阻塞段在线程池):libwebrtc 初始化 + nativecore 抓屏
            width, height = await session.open()
            await ws.send_json(protocol.make_message(
                "connected",
                session_id=session_id,
                server_caps={
                    "codecs": ["h264", "vp8"],
                    "audio": False,
                    "max_sessions": self.max_sessions,
                }))
            # 就绪信息:分辨率以实际捕获为准
            await ws.send_json(protocol.make_message(
                "ready",
                resolution={"width": width, "height": height},
                codec="h264/vp8",
                bitrate_kbps=0,
                source="desktop"))
            logger.info("会话 %s 建立(client=%s, %dx%d)", session_id, client_info,
                        width, height)
            # 重要:必须先发 connected/ready 再发 offer,
            # 客户端依赖 connected 才创建 RTCPeerConnection(见 JS SDK)
            await session.send_offer()

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        message = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await session.handle_message(message)
                elif msg.type == WSMsgType.ERROR:
                    break
        except Exception as exc:
            logger.exception("会话异常")
            try:
                await ws.send_json(protocol.make_message(
                    "error", code=protocol.ERR_INTERNAL, message=str(exc), fatal=True))
            except Exception:
                pass
        finally:
            self._sessions.pop(session_id, None)
            self._session_clients.pop(session_id, None)
            self._session_ws.pop(session_id, None)
            if session is not None:
                # close 内含 DLL 侧 join 等阻塞段,限时兜底防挂起
                closer = asyncio.create_task(session.close())
                _done, _pending = await asyncio.wait({closer}, timeout=25.0)
                if closer not in _done:
                    logger.warning("会话 %s 关闭超时(转后台继续)", session_id)
        return ws

    def _make_sender(self, ws: web.WebSocketResponse):
        async def send(message: dict) -> None:
            if not ws.closed:
                await ws.send_json(message)
        return send

    def _make_closed_notifier(self, ws: web.WebSocketResponse):
        """DLL 侧会话结束(ICE failed/closed 等)→ 通知客户端并关闭 ws。"""
        async def notify(reason: str) -> None:
            try:
                if not ws.closed:
                    await ws.send_json(protocol.make_message(
                        "close", reason=reason or "closed"))
                    await ws.close()
            except Exception:
                logger.debug("close 通知发送失败", exc_info=True)
        return notify

    async def _kick_duplicate_sessions(self, remote: str) -> None:
        """同客户端重复连接治理(与 aiortc 版一致;等待踢除完成以便上限重判)。"""
        if not remote:
            return
        stale = [(sid, s) for sid, s in self._sessions.items()
                 if self._session_clients.get(sid) == remote]
        for sid, s in stale:
            logger.info("同客户端(%s)重复连接:踢除旧会话 %s", remote, sid)
            await self._kick_one(sid, s)

    async def _kick_one(self, sid: str, session: CppPeerSession) -> None:
        ws = self._session_ws.get(sid)
        try:
            await session.close()
        except Exception:
            logger.warning("踢除会话 %s:关闭异常", sid, exc_info=True)
        if ws is not None and not ws.closed:
            try:
                await ws.close(code=1000, message=b"replaced by new session")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 锁屏画面(安全桌面)worker 生命周期(对齐 aiortc 版 server.py)
    # ------------------------------------------------------------------
    async def _on_startup(self, app: web.Application) -> None:
        """启动:spawn 安全桌面 worker(锁屏密码界面抓帧/输入);失败降级。"""
        if sys.platform != "win32":
            return
        # 端口隔离 + token 认证:worker 仅本进程可连
        wdesktop.set_port(self.wdesktop_port)
        self._wdesktop_token = secrets.token_hex(8)
        wdesktop.set_token(self._wdesktop_token)
        await self._spawn_wdesktop()
        self._wdesktop_keeper_task = asyncio.create_task(
            self._wdesktop_keeper_loop())

    async def _on_cleanup(self, app: web.Application) -> None:
        if self._wdesktop_keeper_task is not None:
            self._wdesktop_keeper_task.cancel()
            try:
                await self._wdesktop_keeper_task
            except asyncio.CancelledError:
                pass

    async def _spawn_wdesktop(self) -> None:
        """以 winlogon 令牌 spawn worker 并自检;记录 spawn 时间(节流用)。"""
        self._wdesktop_spawn_ts = time.monotonic()
        # 独立日志便于验证出生桌面/输入通道;其 ".reload" 热更新标记也
        # 与 8080 的 worker 分离,避免互相干扰
        log_path = os.path.join(os.getcwd(), "_wdesktop_worker_cpp.log")
        pid = await asyncio.to_thread(
            spawn_wdesktop_worker, wdesktop.get_port(),
            self._wdesktop_token, log_path)
        if not pid:
            logger.warning("锁屏画面 worker 启动失败(服务端需管理员运行):"
                           "锁屏时将只推静止帧,无法在浏览器输入密码")
            return
        ok = False
        for _ in range(10):
            await asyncio.sleep(0.3)
            ok = await asyncio.to_thread(wdesktop.ping)
            if ok:
                break
        if not ok:
            logger.warning("锁屏画面 worker(pid=%d)未就绪,稍后由 keeper 重试",
                           pid)
            return
        probe = await asyncio.to_thread(wdesktop.probe)
        logger.info("锁屏画面 worker 就绪(pid=%d,端口=%d):锁屏后密码界面将在"
                    "浏览器端实时可见;抓帧自检=%s", pid, wdesktop.get_port(),
                    "OK" if probe else "未通过(锁屏后再试)")

    async def _wdesktop_keeper_loop(self) -> None:
        """守护 worker:每 10s 探活;离线且距上次 spawn ≥30s 时重拉。"""
        try:
            while True:
                await asyncio.sleep(10.0)
                if await asyncio.to_thread(wdesktop.ping):
                    continue
                if time.monotonic() - self._wdesktop_spawn_ts < 30.0:
                    continue
                logger.warning("锁屏画面 worker 离线,重新拉取")
                await self._spawn_wdesktop()
        except asyncio.CancelledError:
            pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="CloudRender 信令壳(媒体核心:cloudrender_session.dll)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--token", default=None, help="连接鉴权 token(缺省不校验)")
    parser.add_argument("--max-sessions", type=int, default=1,
                        help="并发会话上限(nativecore 抓屏同显示器互斥,默认 1)")
    parser.add_argument("--fps", type=int, default=30, help="帧率上限 1..60")
    parser.add_argument("--bitrate", type=int, default=10000,
                        help="视频目标码率 kbps(固定值,默认 10000 对齐 8080 "
                             "运行配置;过低画面发糊;0=自适应)")
    parser.add_argument("--monitor", type=int, default=-1, help="-1=主显示器")
    parser.add_argument("--window", type=lambda v: int(v, 0), default=0,
                        help="捕获窗口句柄(如 0x1A2B3C);非 0 时按窗口捕获")
    parser.add_argument("--ice", action="append", default=None,
                        help="STUN 服务器(可多次,如 --ice stun:host:3478);"
                             "缺省=局域网直连(host 候选)")
    parser.add_argument("--dll", default=None,
                        help="cloudrender_session.dll 路径(缺省自动探测构建产物)")
    parser.add_argument("--wdesktop-port", type=int, default=45995,
                        help="安全桌面(锁屏)worker 端口(默认 45995,"
                             "与 aiortc 版 45990 隔离)")
    args = parser.parse_args()

    server = CppCloudRenderServer(
        host=args.host, port=args.port, token=args.token,
        max_sessions=args.max_sessions, fps=args.fps,
        bitrate_kbps=args.bitrate, monitor=args.monitor,
        window_hwnd=args.window, ice_servers=args.ice, dll_path=args.dll,
        wdesktop_port=args.wdesktop_port)
    logger.info("CloudRender C++ 会话核心信令壳启动: http://%s:%d "
                "(内置 Web UI,页面与信令同端口)", args.host, args.port)
    try:
        server.run()
    except OSError as exc:
        if exc.errno == 10048:  # WSAEADDRINUSE
            logger.error("端口 %d 已被占用:请先关闭正在运行的服务端实例,"
                         "或用 --port 指定其他端口后重试", args.port)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()