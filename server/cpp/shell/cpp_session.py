"""CppPeerSession:cloudrender_session.dll 会话的异步封装。

与 session.py(aiortc 版) 同一职责:建立抓屏会话、生成 offer、消费客户端
信令、把 DLL 侧信令事件(ice/closed)经 on_signal 交上层发送。差异:
WebRTC/注入全部在 DLL 内完成,本层只做信令桥接与轮询。

线程模型:cr_session_create/create_offer/close 为阻塞调用,统一经
asyncio.to_thread 执行;poll_signal 为非阻塞轻量调用,直接在事件循环内轮询。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Awaitable, Callable, Optional

# 信令壳与全部依赖模块平铺于本目录,直接 import(自包含,无 server/python 依赖)
import capi
import protocol
import secure_ops
import wdesktop

logger = logging.getLogger("cloudrender.cpp_session")

SignalSender = Callable[[dict], Awaitable[None]]
ClosedNotifier = Callable[[str], Awaitable[None]]


class CppPeerSession:
    def __init__(self, config: dict,
                 on_signal: SignalSender,
                 on_closed: ClosedNotifier,
                 dll_path: Optional[str] = None,
                 poll_interval: float = 0.02,
                 stats_interval: float = 2.0,
                 bitrate_interval: float = 1.0):
        self.config = config
        self.on_signal = on_signal
        self.on_closed = on_closed
        self.poll_interval = poll_interval
        self.stats_interval = stats_interval
        self.bitrate_interval = bitrate_interval
        self.bitrate_kbps = int(config.get("bitrate_kbps", 0) or 0)
        self.fps = int(config.get("fps", 30) or 30)

        self._lib = capi.load_dll(dll_path)
        self._cr: Optional[capi.CrSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._bitrate_task: Optional[asyncio.Task] = None
        self._guard_task: Optional[asyncio.Task] = None       # 锁屏/UIPI 守护
        self._secure_frame_task: Optional[asyncio.Task] = None  # 锁屏帧桥
        self._secure_on = False        # 当前是否处于安全桌面(锁屏)模式
        self._high_il_notify_ts = 0.0  # 高完整性窗口提示节流(monotonic 秒)
        self._closed = False
        self._seq_next = itertools.count(1).__next__
        self.resolution: tuple[int, int] = (0, 0)
        # 统计差分基线
        self._stats_last_bytes = 0
        self._stats_last_frames = 0
        self._stats_last_ts = 0.0

    # ------------------------------------------------------------------
    async def open(self) -> tuple[int, int]:
        """创建 DLL 会话(libwebrtc 初始化 + nativecore 抓屏)并启动轮询。

        返回实际捕获分辨率。阻塞部分在线程池执行,不卡事件循环。
        """
        self._cr = await asyncio.to_thread(capi.CrSession, self._lib, self.config)
        self.resolution = self._cr.source_size()
        self._poll_task = asyncio.create_task(self._poll_loop())
        return self.resolution

    async def send_offer(self) -> None:
        """生成并发送 offer(应在 connected/ready 之后调用,与 aiortc 版一致)。"""
        if self._cr is None:
            raise RuntimeError("session is not open")
        sdp = await asyncio.to_thread(self._cr.create_offer)
        await self._emit("offer", sdp=sdp)
        # 与 aiortc 版一致:offer 发出后启动统计上报
        if self._stats_task is None:
            self._stats_task = asyncio.create_task(self._stats_loop())
        # 目标码率:运行期周期重申(带宽估计起步偏低,单次设置会被拖糊)
        if self._bitrate_task is None and self.bitrate_kbps > 0:
            self._bitrate_task = asyncio.create_task(self._bitrate_loop())
        # 锁屏/UIPI 守护:检测安全桌面切换并管理锁屏帧桥与 toast 提示
        if self._guard_task is None:
            self._guard_task = asyncio.create_task(self._guard_loop())

    async def _emit(self, msg_type: str, **payload) -> None:
        if not self._closed:
            await self.on_signal(protocol.make_message(msg_type, self._seq_next(), **payload))

    # ------------------------------------------------------------------
    async def handle_message(self, message: dict) -> None:
        """客户端信令分发(由信令壳调用)。"""
        if self._closed or self._cr is None:
            return
        mtype = message.get("type")
        payload = message.get("payload") or {}
        if mtype in ("answer", "ice"):
            rc = await asyncio.to_thread(self._cr.handle_signal, mtype, payload)
            if rc != capi.OK:
                logger.warning("handle_signal(%s) 返回 %d", mtype, rc)
        elif mtype == "lock_screen":
            await self._lock_screen()
        elif mtype == "disconnect":
            await self.close()
        # 其余(stats/ping/video_lost)暂不支持,DLL 侧静默忽略

    async def _lock_screen(self) -> None:
        """锁定远端桌面(客户端"锁屏"按钮):等效 Win+L,一步进入锁屏界面。

        锁屏后画面/输入由安全桌面通道(worker)接管,可在浏览器中直接输入
        密码解锁。Ctrl+Alt+Del(SAS)序列被系统禁止程序模拟,故用官方 API
        LockWorkStation 实现;服务端须运行于交互式会话。
        """
        if await asyncio.to_thread(secure_ops.lock_workstation):
            logger.info("收到锁屏请求:已锁定桌面(等效 Win+L)")
        else:
            logger.warning("锁屏请求失败:服务端不在交互式会话")
            self._send_toast("Lock failed: the server is not in an interactive desktop session.")

    def _send_toast(self, text: str) -> bool:
        """经 events 数据通道(DLL 侧)向客户端发一条提示;返回是否已发出。"""
        cr = self._cr
        if cr is None or self._closed:
            return False
        try:
            return cr.send_toast(text)
        except Exception:
            logger.debug("toast 发送失败", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 锁屏(安全桌面)切换:帧/输入通道由 worker 接管
    # ------------------------------------------------------------------
    async def _set_secure(self, on: bool) -> None:
        """切换 DLL 安全桌面模式并管理锁屏帧桥(幂等)。"""
        cr = self._cr
        if cr is None or self._closed or self._secure_on == on:
            return
        # CrSession.set_secure_mode 返回 bool(封装内已做 ==OK 归一),并非
        # C ABI 原始返回码;按 rc!=OK 判断会把成功的 True 误判为失败,
        # 导致帧桥不启动且 _secure_on 不置位(解锁时 DLL 无法恢复)
        ok = await asyncio.to_thread(cr.set_secure_mode, on)
        if not ok:
            logger.warning("set_secure_mode(%s) 失败: DLL 未就绪", on)
            return
        self._secure_on = on
        if on:
            if self._secure_frame_task is None:
                self._secure_frame_task = asyncio.create_task(
                    self._secure_frame_loop())
        else:
            task, self._secure_frame_task = self._secure_frame_task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _secure_frame_loop(self) -> None:
        """锁屏帧桥:周期从 SYSTEM worker 抓安全桌面帧并注入 DLL 帧泵。

        普通权限进程锁屏期间无法抓屏,画面由 worker 提供;抓取失败本轮跳过
        (帧泵重复最后一帧,画面静止而非黑屏)。
        """
        w, h = self.resolution
        if w <= 0 or h <= 0:
            return
        region = {"left": 0, "top": 0, "width": w, "height": h}
        period = 1.0 / max(1, min(30, self.fps))
        pushed = grab_fail = push_fail = 0
        logger.info("锁屏帧桥启动: 目标 %dx%d 周期 %.0fms", w, h, period * 1000)
        try:
            while not self._closed:
                frame = await asyncio.to_thread(wdesktop.grab_secure, region)
                cr = self._cr
                if frame is not None and cr is not None and not self._closed:
                    if pushed == 0:
                        # 诊断:首帧亮度采样(≈0 表示 worker 抓到纯黑画面,
                        # 据此区分"抓帧内容问题"与"下游渲染问题")
                        sample = float(frame[::32, ::32].mean())
                        logger.info("锁屏帧桥: 首帧 %dx%d 亮度采样=%.1f",
                                    frame.shape[1], frame.shape[0], sample)
                    ok = await asyncio.to_thread(
                        cr.push_secure_frame, frame.tobytes(), w, h, w * 4,
                        time.time_ns() // 1000)
                    if ok:
                        pushed += 1
                        if pushed % 300 == 1:
                            logger.info("锁屏帧桥: 已注入 %d 帧(抓取失败 %d,"
                                        "注入失败 %d)",
                                        pushed, grab_fail, push_fail)
                    else:
                        push_fail += 1
                        if push_fail == 1:
                            logger.warning("锁屏帧桥: push_secure_frame 返回失败")
                else:
                    grab_fail += 1
                    if grab_fail == 1:
                        logger.warning("锁屏帧桥: worker 抓帧失败(本帧返回 None)")
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            logger.info("锁屏帧桥停止: 已注入 %d 帧(抓取失败 %d, 注入失败 %d)",
                        pushed, grab_fail, push_fail)
            raise
        except Exception:
            logger.exception("锁屏帧桥异常退出")

    async def _guard_loop(self) -> None:
        """监控安全桌面(锁屏/UAC)切换与 UIPI(高权限窗口)状态。

        锁屏 → 切 DLL 安全帧模式 + 启动 worker 帧桥(锁屏画面/密码输入由
        SYSTEM worker 承担);解锁 → 恢复本机抓屏;期间经 toast 提示客户端。
        锁屏期间跳过 UIPI 检查(输入由 worker 注入安全桌面)。
        """
        locked = False
        lock_live = False       # 当前锁屏提示是否为"已接入画面"版本
        next_live_check = 0.0
        try:
            while not self._closed:
                secure = await asyncio.to_thread(secure_ops.secure_desktop_active)
                if secure:
                    now = time.monotonic()
                    if not locked:
                        await self._set_secure(True)
                        available = await asyncio.to_thread(wdesktop.ping)
                        if self._send_toast(
                                "Desktop locked; lock screen feed is live. Type your password to unlock."
                                if available else
                                "Desktop locked; video is paused and will resume after unlock."):
                            locked = True
                            lock_live = available
                            next_live_check = now + 5.0
                            logger.info("检测到安全桌面(锁屏):已切换安全帧模式"
                                        "(锁屏画面通道=%s)",
                                        "可用" if available else "不可用")
                    elif not lock_live and now >= next_live_check:
                        next_live_check = now + 5.0
                        if await asyncio.to_thread(wdesktop.ping):
                            if self._send_toast(
                                    "Lock screen feed is ready. Type your password to unlock."):
                                lock_live = True
                                logger.info("锁屏画面通道恢复:已提示客户端")
                    await asyncio.sleep(1.5)
                    continue
                if locked:
                    await self._set_secure(False)
                    if self._send_toast("Unlocked; video is resuming."):
                        locked = False
                        logger.info("已退出安全桌面(解锁):恢复本机抓屏")
                blocked = await asyncio.to_thread(
                    secure_ops.foreground_blocks_injection)
                if blocked:
                    now = time.monotonic()
                    if now - self._high_il_notify_ts >= 30.0:
                        if self._send_toast(
                                "A higher-privilege window (e.g. Task Manager) is focused: "
                                "Windows blocks input from non-admin processes, so clicks "
                                "will not respond. Run the server as administrator and retry."):
                            self._high_il_notify_ts = now
                            logger.warning(
                                "前台为高完整性窗口,UIPI 会丢弃注入:已提示客户端")
                elif blocked is False:
                    self._high_il_notify_ts = 0.0  # 恢复后重置,下次进入立即提示
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("锁屏守护异常退出")

    async def _handle_secure_input(self, payload: dict) -> None:
        """安全桌面输入:DLL 旁路转发的原始输入帧 → 经 worker 注入。

        锁屏期间普通进程 SendInput 到不了安全桌面,DLL 不解码注入而是把
        原始帧经信令旁路到这里(对齐 aiortc 版 _apply_input_secure)。
        为防密码泄漏,键盘/文本一律不写日志。
        """
        hex_data = str(payload.get("data") or "")
        if not hex_data:
            return
        try:
            _seq, events = protocol.decode_input_frame(bytes.fromhex(hex_data))
        except Exception:
            logger.debug("非法安全输入帧", exc_info=True)
            return
        for ev in events:
            try:
                await self._apply_input_secure(ev)
            except Exception:
                logger.debug("安全输入注入失败", exc_info=True)

    async def _apply_input_secure(self, ev) -> None:
        """锁屏输入路由:全部经 SYSTEM worker 注入 Winlogon 桌面。

        坐标不做窗口裁剪(画面即锁屏裁剪区);KIND_TOUCH 在安全桌面下不
        映射(与 aiortc 版一致,密码界面无需触摸手势)。
        """
        w, h = self.resolution
        if ev.kind == protocol.KIND_KEY:
            down, fnv, _mods, _rep = ev.data
            code = protocol.FNV_TO_CODE.get(int(fnv) & 0xFFFFFFFF)
            if code:
                vk = protocol.CODE_TO_VK.get(code)
                if vk:
                    await asyncio.to_thread(wdesktop.send_key, int(vk),
                                            bool(down))
        elif ev.kind == protocol.KIND_MOUSE_MOVE:
            x, y, dx, dy = ev.data
            if x >= 0 and y >= 0:
                await asyncio.to_thread(wdesktop.send_mouse_move,
                                        int(x * w), int(y * h))
            elif dx or dy:
                await asyncio.to_thread(wdesktop.send_mouse_move_rel,
                                        int(dx), int(dy))
        elif ev.kind == protocol.KIND_MOUSE_BUTTON:
            button, down, _clicks = ev.data
            await asyncio.to_thread(wdesktop.send_mouse_button,
                                    int(button), bool(down))
        elif ev.kind == protocol.KIND_WHEEL:
            _dx, dy, _ctrl = ev.data
            await asyncio.to_thread(wdesktop.send_mouse_wheel, int(dy * 120))
        elif ev.kind == protocol.KIND_TEXT:
            await asyncio.to_thread(secure_ops.send_text_secure,
                                    str(ev.data[0]))

    async def _poll_loop(self) -> None:
        """轮询 DLL 信令队列:ice → 转发客户端;closed → 通知上层。"""
        assert self._cr is not None
        try:
            while not self._closed:
                item = self._cr.poll_signal()
                if item is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                mtype = item.get("type")
                payload = item.get("payload") or {}
                if mtype == "ice":
                    await self._emit("ice", **payload)
                elif mtype == "secure_input":
                    await self._handle_secure_input(payload)
                elif mtype == "closed":
                    reason = str(payload.get("reason") or "")
                    logger.info("会话结束:%s", reason or "(无原因)")
                    await self.on_closed(reason)
                    break
                else:  # 未来扩展类型(stats 等):原样透传
                    await self._emit(mtype, **payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("信令轮询异常退出")

    async def _stats_loop(self) -> None:
        """周期上报发送侧统计(与 aiortc 版同格式:clock_ms + video)。

        数据源:DLL GetStats(outbound-rtp bytesSent)+ 帧泵累计帧数;按两次
        采样差分算 fps_sent/bitrate_kbps。首轮成功采样只建差分基线(连接
        建立前帧泵不产帧,差值无意义);未连接轮次推进时间基准。
        """
        assert self._cr is not None
        primed = False
        try:
            while not self._closed:
                await asyncio.sleep(self.stats_interval)
                if self._closed or self._cr is None:
                    continue
                if not self._cr.connected:
                    # 未连接时帧泵未启动:推进时间基准,避免恢复后分母被拉长
                    self._stats_last_ts = time.time()
                    continue
                snap = await asyncio.to_thread(self._cr.get_stats)
                if not snap:
                    continue
                now = time.time()
                bytes_sent = int(snap.get("bytes_sent", 0))
                frames = int(snap.get("frames_pushed", 0))
                if not primed:
                    # 首轮只建基线:分子为"connected 起的累计帧",不能除以全窗口
                    primed = True
                    self._stats_last_bytes = bytes_sent
                    self._stats_last_frames = frames
                    self._stats_last_ts = now
                    continue
                dt = max(1e-6, now - self._stats_last_ts)
                fps = (frames - self._stats_last_frames) / dt
                bitrate = (bytes_sent - self._stats_last_bytes) * 8 / 1000.0 / dt
                self._stats_last_bytes = bytes_sent
                self._stats_last_frames = frames
                self._stats_last_ts = now
                logger.info("发送统计:fps=%.1f kbps=%.1f frames=%d",
                            fps, bitrate, frames)
                await self._emit("stats", clock_ms=int(now * 1000),
                                 video={"fps_sent": round(fps, 1),
                                        "bitrate_kbps": round(bitrate, 1),
                                        "encoder": "h264/vp8"})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("统计上报异常退出")

    # ------------------------------------------------------------------
    async def _bitrate_loop(self) -> None:
        """周期重申目标码率(min=max):带宽估计起步偏低,一次设置会随流启动
        被重置(画面糊十几秒);运行期每秒重申(对齐 aiortc 版 _apply_bitrate
        对抗估计值的模式)。未连接时跳过本轮。"""
        assert self._cr is not None
        try:
            while not self._closed:
                await asyncio.sleep(self.bitrate_interval)
                if self._closed or self._cr is None or not self._cr.connected:
                    continue
                await asyncio.to_thread(self._cr.set_bitrate, self.bitrate_kbps)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("码率重申异常退出")

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """关闭会话(幂等)。"""
        if self._closed:
            return
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass
            self._stats_task = None
        if self._bitrate_task is not None:
            self._bitrate_task.cancel()
            try:
                await self._bitrate_task
            except asyncio.CancelledError:
                pass
            self._bitrate_task = None
        if self._guard_task is not None:
            self._guard_task.cancel()
            try:
                await self._guard_task
            except asyncio.CancelledError:
                pass
            self._guard_task = None
        if self._secure_frame_task is not None:
            self._secure_frame_task.cancel()
            try:
                await self._secure_frame_task
            except asyncio.CancelledError:
                pass
            self._secure_frame_task = None
        if self._cr is not None:
            cr, self._cr = self._cr, None
            await asyncio.to_thread(cr.close)