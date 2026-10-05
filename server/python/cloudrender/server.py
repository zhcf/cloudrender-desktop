"""CloudRender 云桌面服务端 —— CLI 入口 + CloudRenderServer(aiohttp 信令 + 会话编排)。

用法(须以 -m 方式运行):
    python -m cloudrender.server [--host 0.0.0.0] [--port 8080] [--monitor 0]
                                 [--fps 30] [--capture auto|mss|dxgi]

- 默认优先使用 nativecore(DXGI 零拷贝捕获);缺失时回退纯 Python:
  --capture=auto 单显示器优先 DXGI Desktop Duplication(零拷贝),失败/多屏回退 mss 软抓;
- --bitrate:视频目标码率 kbps(默认 6000);过低画面发糊,局域网可上调至 8000-12000;
- 默认捕获整个桌面(全虚拟屏;--monitor 可指定单个显示器);
- 无头(无物理显示器)运行:需先安装/启用虚拟显示器驱动(IddCx 类,分辨率设 1920x1080),
  启动时会检测显示器数量并提示;
- 连接后浏览器打开 http://<host>:<port>/(服务端内置 Web UI,页面与信令同端口)。
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import secrets
import sys
import time
import uuid
from typing import Awaitable, Callable, Optional

from aiohttp import WSMsgType, web

from . import protocol
from . import webui
from . import wdesktop
from .capture import FrameSource, MssScreenSource, NativeCoreCaptureSource
from .inject import InputInjector, NativeCoreInjector, WindowsInputInjector
from .session import PeerSession
from .winlogon import spawn_wdesktop_worker

logger = logging.getLogger("server")

SourceFactory = Callable[[dict], FrameSource]
InjectorFactory = Callable[[dict], InputInjector]


def _raise_timer_resolution() -> None:
    """Windows: 将本进程系统定时器分辨率提升到 1ms。

    默认粒度 15.625ms 会把 asyncio.sleep(1/60)(16.7ms) 拖到 ~31ms,
    60fps 节拍被死锁在 ~30fps;提升后高帧率推流节拍才能达成。
    仅影响本进程,进程退出后系统自动释放该请求。
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        logger.debug("系统定时器分辨率已提升到 1ms")
    except Exception:
        logger.warning("timeBeginPeriod(1) 调用失败,高帧率节拍可能不达预期",
                       exc_info=True)


def _display_count() -> int:
    """活跃显示器数量(SM_CMONITORS);无头且无虚拟显示器时可能为 0。"""
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(80))
    except Exception:
        return 1


class CloudRenderServer:
    """aiohttp WebSocket 信令服务器 + 会话编排。

    编程式用法:

        server = CloudRenderServer(port=8080)
        server.run(source_factory, injector_factory)
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080,
                 token: Optional[str] = None, max_sessions: int = 1,
                 fps: int = 30, stats_interval: float = 2.0,
                 bitrate: int = 6_000_000,
                 ice_servers: Optional[list] = None):
        self.host = host
        self.port = port
        self.token = token
        # 多会话并发会抓屏失败(nativecore DXGI 互斥),故默认 1(与 C++ 版一致;
        # 需要多会话时先做共享捕获)
        self.max_sessions = max_sessions
        self.fps = fps
        self.stats_interval = stats_interval
        self.bitrate = bitrate
        self.ice_servers = ice_servers
        self._sessions: dict[str, PeerSession] = {}
        self._session_clients: dict[str, str] = {}   # session_id -> 客户端 IP
        self._session_ws: dict[str, web.WebSocketResponse] = {}
        self._source_factory: Optional[SourceFactory] = None
        self._injector_factory: Optional[InjectorFactory] = None
        # 锁屏画面(安全桌面)worker:启动时 spawn,keeper 任务守护
        self._wdesktop_token = ""
        self._wdesktop_spawn_ts = 0.0
        self._wdesktop_log_path = os.path.join(os.getcwd(),
                                               "_wdesktop_worker.log")
        self._wdesktop_keeper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    def build_app(self, source_factory: SourceFactory,
                  injector_factory: Optional[InjectorFactory] = None) -> web.Application:
        self._source_factory = source_factory
        self._injector_factory = injector_factory or (lambda _info: None)
        app = web.Application()
        app["cloudrender_server"] = self
        app.router.add_get("/ws", self._ws_handler)
        # 内置 Web UI:页面挂在 /(与 /ws 同端口);web 目录缺失时仅信令可用
        webui.install_web_routes(app)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    def run(self, source_factory: SourceFactory,
            injector_factory: Optional[InjectorFactory] = None,
            **run_kwargs) -> None:
        """阻塞式运行(内部启动 aiohttp)。"""
        _raise_timer_resolution()
        app = self.build_app(source_factory, injector_factory)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        web.run_app(app, host=self.host, port=self.port, **run_kwargs)

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
        first = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        if first.get("type") != "connect":
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_BAD_PARAM, message="first message must be 'connect'", fatal=True))
            await ws.close()
            return ws
        client_info = (first.get("payload") or {}).get("client_info") or {}

        session_id = uuid.uuid4().hex[:12]
        session = None
        remote = request.remote or ""
        # 同客户端重复连接治理:云桌面单客户端只应有一条活跃会话——
        # 浏览器重连风暴会产生多条并存会话,各自抓屏/编码互挤 CPU
        # (fps 掉到十几),且旧会话可能长期占用 DXGI 高速通道。
        # 新连接到来时把同 IP 的旧会话全部踢除(关会话+关 ws)。
        # 注意:必须先 await 踢除完成、再做上限检查——否则 max_sessions=1
        # 时同 IP 重连会被旧会话占位误判为"会话数已达上限"。
        await self._kick_duplicate_sessions(remote)
        if len(self._sessions) >= self.max_sessions:
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_MAX_SESSIONS, message="session limit reached", fatal=True))
            await ws.close(code=4402)
            return ws

        try:
            source = self._source_factory(client_info)
            injector = (self._injector_factory(client_info)
                        if self._injector_factory else None)
            if injector is None:
                raise RuntimeError("no InputInjector provided (injector_factory returned None)")

            session = PeerSession(
                source=source,
                injector=injector,
                on_signal=self._make_sender(ws),
                on_closed=lambda: self._release(session_id, session),
                fps=self.fps,
                bitrate=self.bitrate,
                ice_servers=self.ice_servers,
                stats_interval=self.stats_interval,
            )
            self._sessions[session_id] = session
            self._session_clients[session_id] = remote
            self._session_ws[session_id] = ws
            await session.open()

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
                resolution={"width": source.width, "height": source.height},
                codec="h264/vp8",
                bitrate_kbps=0,
                source="desktop"))
            logger.info("会话 %s 建立(client=%s)", session_id, client_info)
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
        except asyncio.TimeoutError:
            await ws.send_json(protocol.make_message(
                "error", code=protocol.ERR_BAD_PARAM, message="connect timed out", fatal=True))
        except Exception as exc:
            logger.exception("会话异常")
            try:
                await ws.send_json(protocol.make_message(
                    "error", code=protocol.ERR_INTERNAL, message=str(exc), fatal=True))
            except Exception:
                pass
        finally:
            if session is not None:
                # close() 内部分段限时;这里再套一层总时间盒:极端情况下
                # (aiortc 关闭协程不响应取消等)也保证 handler 能退出,
                # 超时任务转后台继续,会话登记由下方 pop 兜底回收
                closer = asyncio.create_task(session.close())
                _done, _pending = await asyncio.wait({closer}, timeout=25.0)
                if closer not in _done:
                    logger.warning("会话 %s 关闭超时(转后台继续)", session_id)
                self._sessions.pop(session_id, None)
                self._session_clients.pop(session_id, None)
                self._session_ws.pop(session_id, None)
        return ws

    @staticmethod
    def _make_sender(ws: web.WebSocketResponse) -> Callable[[dict], Awaitable[None]]:
        async def send(message: dict) -> None:
            if not ws.closed:
                await ws.send_json(message)
        return send

    async def _release(self, session_id: str, session: PeerSession) -> None:
        self._sessions.pop(session_id, None)
        self._session_clients.pop(session_id, None)
        logger.info("会话 %s 已释放(当前 %d 个)", session_id, len(self._sessions))

    async def _kick_duplicate_sessions(self, remote: str) -> None:
        """同客户端重复连接治理(与 C++ 版一致;等待踢除完成以便上限重判)。"""
        if not remote:
            return
        stale = [(sid, s) for sid, s in self._sessions.items()
                 if self._session_clients.get(sid) == remote]
        for sid, s in stale:
            logger.info("同客户端(%s)重复连接:踢除旧会话 %s", remote, sid)
            await self._kick_one(sid, s)

    async def _kick_one(self, sid: str, session: PeerSession) -> None:
        """踢除单条旧会话:关闭会话(释放抓屏/PC)并关闭其 ws。"""
        ws = self._session_ws.get(sid)
        try:
            await session.close()
        except Exception:
            logger.warning("踢除会话 %s:关闭异常", sid, exc_info=True)
        if ws is not None and not ws.closed:
            try:
                await ws.close(code=1000,
                               message=b"replaced by new session")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 锁屏画面(安全桌面)worker 生命周期
    # ------------------------------------------------------------------
    async def _on_startup(self, app: web.Application) -> None:
        """启动:spawn 安全桌面 worker(锁屏密码界面抓帧/输入);失败降级。"""
        if sys.platform != "win32":
            return
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
        pid = await asyncio.to_thread(
            spawn_wdesktop_worker, wdesktop.WDESKTOP_PORT,
            self._wdesktop_token, self._wdesktop_log_path)
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
        logger.info("锁屏画面 worker 就绪(pid=%d):锁屏后密码界面将在浏览器端"
                    "实时可见;抓帧自检=%s", pid,
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="CloudRender 云桌面 Demo")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--monitor", type=int, default=0,
                        help="捕获显示器索引:0=整个桌面(默认),1=主显示器,2..=其余显示器")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=6000,
                        help="视频目标码率 kbps(默认 6000,1080p 桌面清晰所需;"
                             "过低会糊,局域网可上调)")
    parser.add_argument("--token", default=None, help="可选鉴权 token(ws?token=...)")
    parser.add_argument("--max-sessions", type=int, default=1,
                        help="最大并发会话数(默认 1;nativecore 抓屏互斥,多会话需先做共享捕获)")
    parser.add_argument("--capture", choices=["auto", "mss", "dxgi"], default="auto",
                        help="抓屏后端:auto=单显示器优先 DXGI 零拷贝,失败/多屏回退 mss;"
                             "mss=强制 GDI 软抓;dxgi=强制 DXGI Desktop Duplication")
    parser.add_argument("--force-fallback", action="store_true",
                        help="等价 --capture mss(强制 mss 软抓,不使用 nativecore/DXGI)")
    args = parser.parse_args()
    if args.force_fallback:
        args.capture = "mss"

    displays = _display_count()
    if displays <= 0:
        logger.warning("未检测到显示器(无头环境):抓屏无法出画。请先安装/启用"
                       "虚拟显示器驱动(如 IddCx Virtual Display Driver)"
                       "并设为 1920x1080,或接入物理显示器后再启动")
    else:
        logger.info("显示器数量: %d", displays)

    use_native = not args.force_fallback

    def source_factory(client_info):
        if use_native:
            from .capture import find_nativecore
            if find_nativecore() is not None:
                return NativeCoreCaptureSource(monitor=args.monitor,
                                               max_fps=args.fps)
            logger.warning("nativecore.dll 未找到,回退纯 Python 捕获(--capture=%s)",
                           args.capture)
        return MssScreenSource(monitor=args.monitor, max_fps=args.fps,
                               backend=args.capture)

    def injector_factory(_client_info):
        try:
            return NativeCoreInjector()
        except RuntimeError:
            return WindowsInputInjector()

    server = CloudRenderServer(host=args.host, port=args.port, token=args.token,
                               max_sessions=args.max_sessions, fps=args.fps,
                               bitrate=args.bitrate * 1000)
    logger.info("CloudRender 云桌面 Demo 启动: http://%s:%d "
                "(内置 Web UI,页面与信令同端口)", args.host, args.port)
    try:
        server.run(source_factory, injector_factory)
    except OSError as exc:
        if exc.errno == 10048:  # WSAEADDRINUSE
            logger.error("端口 %d 已被占用:请先关闭正在运行的服务端实例,"
                         "或用 --port 指定其他端口后重试", args.port)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()