"""PeerSession:单个客户端的 WebRTC 会话。

职责:创建 RTCPeerConnection(服务端 offer)、挂视频轨、创建 input/events 数据通道、
分发输入事件给 InputInjector、周期统计上报与关键帧请求处理。
"""
from __future__ import annotations

import asyncio
import ctypes
import itertools
import logging
import sys
import time
from typing import Awaitable, Callable, Optional

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.rtcicetransport import RTCIceCandidate

from . import protocol
from . import wdesktop
from .capture import FrameSource, secure_desktop_active
from .inject import InputInjector
from .streamer import CaptureVideoTrack

logger = logging.getLogger("cloudrender.session")

RTP_CODECS = ("H264", "VP8")  # 服务端 offer 偏好顺序

_seq_next = itertools.count(1).__next__

# --- aiortc 码率修复补丁 ------------------------------------------------
# 三个缺陷使 Python(aiortc) 侧画面远糊于 C++ 侧(min=max 固定 10Mbps):
# 1) H264Encoder target_bitrate 的 setter 把值 clamp 到 MAX_BITRATE=3Mbps,
#    会话目标(如 10Mbps)实际被截为 3Mbps;VP8(vpx) 更低,仅 1.5Mbps;
# 2) REMB 带宽估计(局域网/回环下偏保守)到达时,会把 target_bitrate 直接
#    覆盖为估计值;
# 3) 编码器检测到"目标与当前差超 10%"即销毁重建,由 2 引发的覆盖会造成
#    周期性重建、丢失参考帧,平均码率进一步走低。
# 修复:按 C++ 侧 min=max 的哲学,解除 clamp 上限并让目标值钉住——REMB
# 的覆盖在同一个 RTCP 处理片段内被立即纠正,编码器观察不到变化。
_H264_MAX_BITRATE = 20_000_000   # bps:覆盖 aiortc 3Mbps 上限
_VP8_MAX_BITRATE = 20_000_000    # bps:覆盖 aiortc 1.5Mbps 上限
_REMB_FLOOR: dict[int, int] = {}  # id(sender) -> 运行期不允许被下压的目标码率


def _install_aiortc_bitrate_patches() -> None:
    """安装 aiortc 码率补丁(幂等,导入本模块时执行一次)。"""
    from aiortc.codecs import h264 as _h264
    from aiortc.codecs import vpx as _vpx
    _h264.MAX_BITRATE = _H264_MAX_BITRATE
    _vpx.MAX_BITRATE = _VP8_MAX_BITRATE

    from aiortc.rtcrtpsender import RTCRtpSender
    if getattr(RTCRtpSender, "_cloudrender_rate_patched", False):
        return
    _orig_handle_rtcp = RTCRtpSender._handle_rtcp_packet

    async def _handle_rtcp_packet(self, packet) -> None:
        await _orig_handle_rtcp(self, packet)
        # REMB 命中时 target_bitrate 已被覆盖;覆盖与纠正之间没有 await,
        # 编码任务无从插入,编码器不会观察到 >10% 变化而触发重建
        floor = _REMB_FLOOR.get(id(self))
        if floor is None:
            return
        encoder = getattr(self, "_RTCRtpSender__encoder", None)
        if (encoder is not None and hasattr(encoder, "target_bitrate")
                and encoder.target_bitrate != floor):
            encoder.target_bitrate = floor

    RTCRtpSender._handle_rtcp_packet = _handle_rtcp_packet
    RTCRtpSender._cloudrender_rate_patched = True


_install_aiortc_bitrate_patches()

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


def _foreground_blocks_injection() -> Optional[bool]:
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


def _lock_workstation() -> bool:
    """锁定当前交互式工作站(等效 Win+L);非交互会话下调用失败,返回 False。"""
    try:
        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:
        logger.debug("LockWorkStation 调用失败", exc_info=True)
        return False


class PeerSession:
    def __init__(self, source: FrameSource, injector: InputInjector,
                 on_signal: Callable[[dict], Awaitable[None]],
                 on_closed: Callable[[], Awaitable[None]],
                 fps: int = 30,
                 bitrate: int = 6_000_000,
                 ice_servers: Optional[list] = None,
                 stats_interval: float = 2.0):
        self.source = source
        self.injector = injector
        self.on_signal = on_signal
        self.on_closed = on_closed
        self.fps = fps
        self.bitrate = bitrate
        self.stats_interval = stats_interval

        # 默认不配 STUN:局域网/直连场景 host 候选即可连通;公共 STUN 不可达时
        # ICE 收集要等到超时(实测 setLocalDescription 卡约 5s),拖慢首帧。
        # 需要 NAT 穿越的部署显式传入 ice_servers。
        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls=s) for s in (ice_servers or [])])
        self.pc = RTCPeerConnection(configuration=config)
        self.track = CaptureVideoTrack(source, fps=fps)
        self.sender = None
        self._dc_input = None
        self._dc_events = None
        self._closed = False
        self._stats_task: Optional[asyncio.Task] = None
        self._bitrate_task: Optional[asyncio.Task] = None
        self._guard_task: Optional[asyncio.Task] = None
        self._high_il_notify_ts = 0.0  # 高完整性窗口提示节流(monotonic 秒)

        self._bytes_sent = 0
        self._frames_encoded = 0
        self._fps_last_ts = time.time()
        self._last_key_log = 0.0

        # 触摸→鼠标映射状态机(Windows 无通用触摸注入,按 noVNC 惯例降级为鼠标/滚轮)
        self._touches: dict[int, tuple] = {}   # touch id -> (x, y) 归一化坐标
        self._last_cursor: Optional[tuple] = None  # 最近注入的光标桌面坐标(输入裁剪用)
        self._touch_left_down = False          # 单指拖拽模拟的左键状态
        self._touch_right_down = False         # 三指右键状态
        self._touch_scroll = False             # 双指滚动模式
        self._touch_scroll_mid: Optional[tuple] = None  # 双指中点基线

        @self.pc.on("iceconnectionstatechange")
        async def _on_ice_state():
            state = self.pc.iceConnectionState
            logger.info("ice state: %s", state)
            if state in ("failed", "closed"):
                await self.close()
            elif state == "disconnected":
                # 瞬时断连(网络抖动/桌面切换)常能自愈:宽限 8s 后复查,
                # 仍 disconnected 才关闭会话 —— 避免锁屏瞬间的假断连直接
                # 杀掉会话,客户端表现为"连接断掉"
                await asyncio.sleep(8.0)
                if self.pc.iceConnectionState == "disconnected":
                    logger.info("ICE disconnected 超 8s 未恢复,关闭会话")
                    await self.close()

    # ------------------------------------------------------------------
    async def open(self) -> None:
        """准备捕获、挂轨、建数据通道;不发送 offer。"""
        await self.source.start()
        self.sender = self.pc.addTrack(self.track)
        # 注册码率下限:REMB 下压将在 RTCP 处理里被立即纠正回目标值
        _REMB_FLOOR[id(self.sender)] = self.bitrate
        # aiortc 编码器惰性创建且默认码率很低(H264 1Mbps/VP8 500kbps),
        # 1080p 桌面内容会明显发糊;等编码器就绪后提升到目标码率
        self._bitrate_task = asyncio.create_task(self._apply_bitrate())
        # 服务端创建两条数据通道
        self._dc_input = self.pc.createDataChannel("input")
        self._dc_events = self.pc.createDataChannel("events")
        self._dc_input.on("message", self._on_input_channel_message)

    async def send_offer(self) -> None:
        """生成并发送 offer(应在 connected/ready 之后调用,保证客户端已建好 RTCPeerConnection)。"""
        offer = await self.pc.createOffer()
        offer.sdp = _prefer_codecs(offer.sdp, RTP_CODECS)
        await self.pc.setLocalDescription(offer)
        await self._signal("offer", sdp=self.pc.localDescription.sdp)
        self._stats_task = asyncio.create_task(self._stats_loop())
        self._guard_task = asyncio.create_task(self._input_guard_loop())

    async def _signal(self, msg_type: str, **payload) -> None:
        if not self._closed:
            await self.on_signal(protocol.make_message(msg_type, _seq_next(), **payload))

    # ------------------------------------------------------------------
    async def handle_message(self, message: dict) -> None:
        """信令消息分发(由 app 调用)。"""
        if self._closed:  # 已关闭/被踢除:忽略残留信令
            return
        mtype = message.get("type")
        payload = message.get("payload") or {}
        if mtype == "answer":
            sdp = payload.get("sdp")
            if isinstance(sdp, dict):
                from aiortc.sdp import SessionDescription
                sdp = SessionDescription(**sdp)
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        elif mtype == "ice":
            candidate = payload.get("candidate")
            if isinstance(candidate, dict):
                sdp = (candidate.get("candidate") or candidate.get("sdp")
                       or candidate.get("candidateStr"))
            else:
                sdp = candidate
            if sdp:
                from aiortc.sdp import candidate_from_sdp
                try:
                    cand = candidate_from_sdp(sdp.split(":", 1)[1] if sdp.startswith("candidate:") else sdp)
                    cand.sdpMid = payload.get("sdpMid")
                    cand.sdpMLineIndex = payload.get("sdpMLineIndex")
                    await self.pc.addIceCandidate(cand)
                except Exception:
                    logger.debug("非法 ICE 候选", exc_info=True)
        elif mtype == "video_lost":
            self.track.force_keyframe()
        elif mtype == "lock_screen":
            await self._lock_screen()
        elif mtype == "disconnect":
            await self.close()
        # stats / ping 上报由 stats_loop 处理

    async def _lock_screen(self) -> None:
        """锁定远端桌面(客户端"锁屏"按钮):等效 Win+L,一步进入锁屏界面。

        锁屏后画面由安全桌面通道接管,可在浏览器中直接输入密码解锁。
        Ctrl+Alt+Del(SAS)序列被系统禁止程序模拟,故用官方 API
        LockWorkStation 实现;服务端须运行于交互式会话。
        """
        if await asyncio.to_thread(_lock_workstation):
            logger.info("收到锁屏请求:已锁定桌面(等效 Win+L)")
        else:
            logger.warning("锁屏请求失败:服务端不在交互式会话")
            self._send_toast("锁屏失败:服务端不在交互式桌面会话中。")

    async def _apply_input(self, ev) -> None:
        # 锁屏/安全桌面(密码输入界面):输入须由 SYSTEM worker 注入
        # Winlogon 桌面(普通进程 SendInput 到不了安全桌面);此路径与
        # 画布窗口是否可见无关,保证浏览器端能输入锁屏密码
        if secure_desktop_active():
            await self._apply_input_secure(ev)
            return
        # 画布模式下窗口不可见时丢弃输入(不会注入到桌面其它应用)
        if not getattr(self.source, "input_active", True):
            return
        # 画布模式:输入裁剪到目标窗口矩形(黑屏区点击不注入被遮挡的其它应用)
        rect = getattr(self.source, "window_rect", None)
        inj = self.injector
        if ev.kind == protocol.KIND_KEY:
            down, fnv, _mods, _rep = ev.data
            code = protocol.FNV_TO_CODE.get(int(fnv) & 0xFFFFFFFF)
            if code:
                vk = protocol.CODE_TO_VK.get(code)
                if vk:
                    now_m = time.monotonic()
                    if now_m - self._last_key_log >= 5.0:
                        self._last_key_log = now_m
                        logger.info("物理键帧: code=%s vk=0x%02X down=%d"
                                    "(若此刻用户未按功能键/组合键,说明前端键盘过滤泄漏)",
                                    code, vk, down)
                    await asyncio.to_thread(inj.inject_key, vk, bool(down))
        elif ev.kind == protocol.KIND_MOUSE_MOVE:
            x, y, dx, dy = ev.data
            if x >= 0 and y >= 0:
                ox, oy = self.source.origin
                px = ox + int(x * self.source.width)
                py = oy + int(y * self.source.height)
                # 相对移动(dx,dy)可能把光标带出窗口,落在窗口外时丢掉;绝对坐标可拉回
                if rect is not None and not self._in_window(px, py, rect):
                    return
                self._last_cursor = (px, py)
                await asyncio.to_thread(inj.inject_mouse_move, px, py)
            elif dx or dy:
                await asyncio.to_thread(inj.inject_mouse_move_rel, int(dx), int(dy))
        elif ev.kind == protocol.KIND_MOUSE_BUTTON:
            if not self._cursor_in_window(rect):
                return
            button, down, _clicks = ev.data
            try:
                await asyncio.to_thread(inj.inject_mouse_button, int(button), bool(down))
            except NotImplementedError:
                logger.debug("injector 不支持按钮 %d", button)
        elif ev.kind == protocol.KIND_WHEEL:
            if not self._cursor_in_window(rect):
                return
            _dx, dy, _ctrl = ev.data
            await asyncio.to_thread(inj.inject_mouse_wheel, int(dy * 120))
        elif ev.kind == protocol.KIND_TOUCH:
            tid, phase, x, y, _pressure = ev.data
            if rect is not None and phase in (0, 1):
                ox, oy = self.source.origin
                if not self._in_window(ox + int(x * self.source.width),
                                       oy + int(y * self.source.height), rect):
                    return  # 黑屏区触摸:丢弃;抬起(phase 2/3)始终放行,防卡键
            await asyncio.to_thread(self._apply_touch_sync,
                                    int(tid), int(phase), float(x), float(y))
        elif ev.kind == protocol.KIND_TEXT:
            logger.info("文本帧: %r", ev.data[0])
            await asyncio.to_thread(inj.inject_text, ev.data[0])

    async def _apply_input_secure(self, ev) -> None:
        """锁屏(安全桌面)输入路由:全部经 SYSTEM worker 注入 Winlogon 桌面。

        与常规路径的差异:坐标不做窗口裁剪(画面即锁屏裁剪区);为防密码
        泄漏,键盘/文本事件一律不写日志。
        """
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
                ox, oy = self.source.origin
                px = ox + int(x * self.source.width)
                py = oy + int(y * self.source.height)
                self._last_cursor = (px, py)
                await asyncio.to_thread(wdesktop.send_mouse_move, px, py)
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
            # 文本帧(兜底通道):逐字符转 vk 注入,不记录内容
            await asyncio.to_thread(self._send_text_secure, str(ev.data[0]))

    @staticmethod
    def _send_text_secure(text: str) -> None:
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

    def _cursor_in_window(self, rect) -> bool:
        """rect 为 None(非画布模式)或尚无已知光标位置时放行;否则要求光标在窗口内。"""
        if rect is None or self._last_cursor is None:
            return True
        return self._in_window(self._last_cursor[0], self._last_cursor[1], rect)

    @staticmethod
    def _in_window(px: int, py: int, rect) -> bool:
        x, y, w, h = rect
        return x <= px < x + w and y <= py < y + h

    def _apply_touch_sync(self, tid: int, phase: int, x: float, y: float) -> None:
        """触摸事件→鼠标注入(同步,在 worker 线程执行;injector 自身线程安全)。

        手势约定(noVNC 惯例,触摸屏/平板直接可用):

        - 单指:tap = 左键点击,拖动 = 左键拖拽;
        - 双指:上下滑动 = 滚轮滚动;
        - 三指:tap = 右键;
        - 手指数变化时切换手势:双指滚屏后抬起一指,剩余单指仅悬停(避免误点);
          三指抬起一指回到双指时重新进入滚动。
        """
        inj = self.injector
        ox, oy = self.source.origin

        def move_at(xx, yy):
            inj.inject_mouse_move(ox + int(xx * self.source.width),
                                  oy + int(yy * self.source.height))

        def mid2():
            xs = [t[0] for t in self._touches.values()]
            ys = [t[1] for t in self._touches.values()]
            return ((xs[0] + xs[1]) / 2, (ys[0] + ys[1]) / 2)

        n0 = len(self._touches)
        if phase in (2, 3):  # up / cancel
            self._touches.pop(tid, None)
        else:
            self._touches[tid] = (x, y)
            if phase == 0:
                move_at(x, y)  # 按下时先把光标定位到触点
        n = len(self._touches)

        if n != n0:
            # ---- 手指数变化:切换手势状态 ----
            if self._touch_right_down and n < 3:
                self._touch_right_down = False
                inj.inject_mouse_button(2, False)
            if n == 0:
                if self._touch_left_down:
                    self._touch_left_down = False
                    inj.inject_mouse_button(0, False)
                self._touch_scroll = False
                self._touch_scroll_mid = None
            elif n == 1:
                self._touch_scroll = False
                self._touch_scroll_mid = None
                if n0 == 0:  # 全新单指:按下左键
                    self._touch_left_down = True
                    inj.inject_mouse_button(0, True)
                # 双指回落单指:仅悬停,不再按左键(防滚屏后误点)
            elif n == 2:
                if self._touch_left_down:
                    self._touch_left_down = False
                    inj.inject_mouse_button(0, False)
                self._touch_scroll = True
                self._touch_scroll_mid = mid2()
            elif n >= 3:
                self._touch_scroll = False
                self._touch_scroll_mid = None
                if not self._touch_right_down:
                    self._touch_right_down = True
                    inj.inject_mouse_button(2, True)
        elif phase == 1:
            # ---- 手指数不变,仅移动 ----
            if n == 1 and self._touch_left_down:
                move_at(x, y)
            elif n == 2 and self._touch_scroll and self._touch_scroll_mid:
                mid = mid2()
                dy = (self._touch_scroll_mid[1] - mid[1]) * self.source.height
                self._touch_scroll_mid = mid
                if abs(dy) >= 1:
                    inj.inject_mouse_wheel(int(dy))  # 指上移=向上滚

    def _on_input_channel_message(self, data) -> None:
        if self._closed:
            return
        try:
            seq, events = protocol.decode_input_frame(data)
            asyncio.ensure_future(self._apply_events(events))
        except Exception:
            logger.debug("非法输入帧", exc_info=True)

    async def _apply_events(self, events) -> None:
        try:
            for ev in events:
                await self._apply_input(ev)
        except Exception:
            logger.exception("input dispatch failed")

    # ------------------------------------------------------------------
    async def _apply_bitrate(self) -> None:
        """维持目标码率:编码器惰性创建(初值 H264 1Mbps/VP8 500kbps)且
        aiortc 默认 clamp 上限极低,等就绪后提升到目标;REMB 下压已由模块
        级补丁即时纠正,此处以 0.25s 节拍兜底复核(也覆盖创建初期窗口)。
        """
        try:
            logged = False
            while not self._closed:
                encoder = getattr(self.sender, "_RTCRtpSender__encoder", None)
                if encoder is not None:
                    if not hasattr(encoder, "target_bitrate"):
                        return
                    if encoder.target_bitrate != self.bitrate:
                        encoder.target_bitrate = self.bitrate
                        if not logged:
                            logger.info("视频目标码率: %d kbps", self.bitrate // 1000)
                            logged = True
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    def _send_toast(self, text: str) -> bool:
        """经 events 数据通道向客户端发一条提示;返回是否已发出。"""
        dc = self._dc_events
        if dc is None or dc.readyState != "open":
            return False
        try:
            dc.send(protocol.encode_event_frame(
                [protocol.ev_toast(text)], _seq_next()))
            return True
        except Exception:
            logger.debug("事件帧发送失败", exc_info=True)
            return False

    async def _input_guard_loop(self) -> None:
        """监控前台窗口完整性(UIPI 会静默丢弃本进程注入,提示客户端,30s
        节流)与安全桌面(锁屏/UAC)切换:锁屏时告知客户端画面状态(worker
        可用 = 锁屏画面已接入,可输密码;不可用 = 静止帧),解锁后提示恢复;
        锁屏期间跳过 UIPI 检查(输入由 worker 注入安全桌面)。"""
        locked = False
        lock_live = False       # 当前锁屏提示是否为"已接入画面"版本
        next_live_check = 0.0
        try:
            while not self._closed:
                secure = await asyncio.to_thread(secure_desktop_active)
                if secure:
                    now = time.monotonic()
                    if not locked:
                        available = await asyncio.to_thread(wdesktop.ping)
                        if self._send_toast(
                                "桌面已锁定,已接入锁屏画面,可直接输入密码解锁。"
                                if available else
                                "桌面已锁定,画面已暂停;解锁后自动恢复。"):
                            locked = True
                            lock_live = available
                            next_live_check = now + 5.0
                            logger.info("检测到安全桌面(锁屏):已提示客户端"
                                        "(锁屏画面通道=%s)",
                                        "可用" if available else "不可用")
                    elif not lock_live and now >= next_live_check:
                        next_live_check = now + 5.0
                        if await asyncio.to_thread(wdesktop.ping):
                            if self._send_toast(
                                    "锁屏画面通道已就绪,可直接输入密码解锁。"):
                                lock_live = True
                                logger.info("锁屏画面通道恢复:已提示客户端")
                    await asyncio.sleep(1.5)
                    continue
                if locked and self._send_toast("已解锁,画面恢复中。"):
                    locked = False
                    logger.info("已退出安全桌面(解锁):已提示客户端")
                blocked = await asyncio.to_thread(_foreground_blocks_injection)
                if blocked:
                    now = time.monotonic()
                    if now - self._high_il_notify_ts >= 30.0:
                        if self._send_toast(
                                "检测到高权限窗口(如任务管理器):系统会阻止"
                                "非管理员进程向它发送输入,点击将无响应。"
                                "请以管理员身份运行服务端后重试。"):
                            self._high_il_notify_ts = now
                            logger.warning(
                                "前台为高完整性窗口,UIPI 会丢弃注入:已提示客户端")
                elif blocked is False:
                    self._high_il_notify_ts = 0.0  # 恢复后重置,下次进入立即提示
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    async def _stats_loop(self) -> None:
        try:
            _n = 0
            while not self._closed:
                await asyncio.sleep(self.stats_interval)
                if self._closed:
                    continue
                payload = {"clock_ms": int(time.time() * 1000)}
                stats = await self.pc.getStats()
                video_rtp = None
                for report in stats.values():
                    if report.type == "outbound-rtp" and getattr(report, "kind", "") == "video":
                        video_rtp = report
                        break
                if video_rtp is not None:
                    ts = time.time()
                    dt = max(1e-6, ts - self._fps_last_ts)
                    bytes_sent = int(getattr(video_rtp, "bytesSent", 0) or 0)
                    # aiortc 的 framesEncoded 在 H264 下可能恒为 0,改用 track 侧帧计数
                    frames = int(getattr(self.track, "_frames", 0))
                    dframes = frames - self._frames_encoded
                    fps = dframes / dt
                    bitrate = (bytes_sent - self._bytes_sent) * 8 / 1000 / dt
                    self._bytes_sent = bytes_sent
                    self._frames_encoded = frames
                    self._fps_last_ts = ts
                    _n += 1
                    if _n % 10 == 0:   # 每 20s 输出一条实际帧率,便于观测
                        logger.info("stats: fps=%.1f 码率=%.0f kbps", fps, bitrate)
                    payload["video"] = {
                        "fps_sent": round(fps, 1),
                        "bitrate_kbps": round(bitrate, 1),
                        "encoder": "?" if not self.track else "h264/vp8",
                        "frame_drop": int(getattr(video_rtp, "framesDiscarded", 0) or 0),
                    }
                await self._signal("stats", **payload)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.sender is not None:
            _REMB_FLOOR.pop(id(self.sender), None)
        if self._stats_task:
            self._stats_task.cancel()
        if self._bitrate_task:
            self._bitrate_task.cancel()
        if self._guard_task:
            self._guard_task.cancel()
        # 先释放帧源(工作线程执行并限时):bettercam release 等阻塞 COM 调用
        # 若在事件循环线程里卡死会冻死整个进程(历史故障:服务端静默失联);
        # 且必须先于 pc.close() 归还 DXGI 占用,否则占用可能因后续卡顿
        # 永不释放,其他会话被迫长期 mss 软抓(fps 掉到十几)
        src_task = asyncio.create_task(
            asyncio.to_thread(self.source.stop_sync))
        _done, _pending = await asyncio.wait({src_task}, timeout=10.0)
        if src_task not in _done:
            logger.warning("释放帧源超时(转后台执行)")
        # 关闭 PeerConnection 限时 8s:aiortc 关闭协程若卡住也不能拖住
        # finally(超时交给后台,下方 on_closed 先完成会话登记回收)
        pc_task = asyncio.create_task(self.pc.close())
        _done, _pending = await asyncio.wait({pc_task}, timeout=8.0)
        if pc_task not in _done:
            logger.warning("关闭 PeerConnection 超时(转后台等待)")
        try:
            await self.on_closed()
        except Exception:
            logger.warning("on_closed 回调异常", exc_info=True)


def _prefer_codecs(sdp: str, codecs) -> str:
    """把指定编解码的 rtpmap 行提前(云桌面场景中 H264 优先)。按媒体段重建,安全可靠。"""
    lines = sdp.split("\r\n")
    result: list = []
    section: list = []

    def flush():
        nonlocal section
        if not section:
            return
        media = section[0]
        attrs = section[1:]
        if not media.startswith("m=video") or len(media.split()) <= 3:
            result.extend(section)
        else:
            parts = media.split()
            pts = parts[3:]
            rtpmap = {}
            fmtp = {}
            for a in attrs:
                if a.startswith("a=rtpmap:"):
                    rtpmap[a[9:a.find(" ")]] = a
                elif a.startswith("a=fmtp:"):
                    fmtp[a[7:a.find(" ")]] = a

            def pref_rank(pt):
                rt = rtpmap.get(pt, "")
                pos = rt.find(" ")
                name = rt[pos + 1:].split("/")[0].lower() if pos >= 0 else ""
                for i, c in enumerate(codecs):
                    if name == c.lower():
                        return i
                return len(codecs)

            ordered = sorted(pts, key=pref_rank)
            result.append(" ".join(parts[:3] + ordered))
            for pt in ordered:
                if pt in rtpmap:
                    result.append(rtpmap[pt])
                    if pt in fmtp:
                        result.append(fmtp[pt])
            result.extend(a for a in attrs
                          if not a.startswith("a=rtpmap:") and not a.startswith("a=fmtp:"))
        section = []

    for line in lines:
        if line.startswith("m=") and section:
            flush()
        section.append(line)
    flush()
    return "\r\n".join(result)