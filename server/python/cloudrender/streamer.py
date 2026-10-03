"""FrameSource → aiortc VideoStreamTrack 适配。

BGRA32 帧 → I420(YUV420P)→ av.VideoFrame。转换主路径用 libswscale(VideoReformatter
复用的 SwsContext,SIMD,1080p 约 2-4ms/帧,numpy 手写版需 35ms+,仅作参考实现)。
静止画面自动补帧(保持 VideoStreamTrack 有帧可推),关键帧策略:首帧强制 IDR,
之后每 5s 一次(供新观众/丢包恢复)。
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import Optional

import numpy as np
from aiortc import MediaStreamError, VideoStreamTrack
from av import VideoFrame
from av.video.reformatter import VideoReformatter

from .capture import CapturedFrame, FrameSource

logger = logging.getLogger("cloudrender.streamer")


def bgra_to_yuv420(frame: CapturedFrame, out: np.ndarray) -> None:
    """BGRA32 → YUV420P(BT.601),向量化。out 形状为 (h*3//2, w) uint8。

    参考实现:纯 numpy,单帧 1080p 约 35ms,仅用于验证/对比;生产路径
    由 CaptureVideoTrack 用 libswscale(VideoReformatter)完成,速度快一个量级。

    PyAV yuv420p 的 ndarray 规范打包:前 h 行为 Y 平面;随后 hh(=h//2) 行
    为色度,每行由两个色度行左右拼接而成 —— 第 0..hh/2 行放 U 平面
    ([U(2r)|U(2r+1)]),第 hh/2..hh 行放 V 平面。色度算术用 int32,避免
    int16 乘加溢出(饱和色下 112*1020 等超出 int16 范围会回绕)。

    frame.bgra 可能是 HxWx4 的 ndarray 或 BGRA bytes(mss 9/10 两层兼容)。
    """
    w, h = frame.width, frame.height
    bgra = frame.bgra
    if isinstance(bgra, (bytes, bytearray, memoryview)):
        bgra = np.frombuffer(bgra, np.uint8, count=h * frame.stride)
    m = np.ascontiguousarray(bgra, dtype=np.uint8).reshape(h, frame.stride)
    m = m[:, : w * 4].reshape(h, w, 4)
    b = m[:, :, 0].astype(np.int32)
    g = m[:, :, 1].astype(np.int32)
    r = m[:, :, 2].astype(np.int32)
    out[:h, :w] = np.clip((66 * r + 129 * g + 25 * b + 128) >> 8, 0, 255).astype(np.uint8) + 16
    # U/V:2x2 像素平均后取 0.5 分辨率
    r2 = r[0::2, 0::2] + r[0::2, 1::2] + r[1::2, 0::2] + r[1::2, 1::2]
    g2 = g[0::2, 0::2] + g[0::2, 1::2] + g[1::2, 0::2] + g[1::2, 1::2]
    b2 = b[0::2, 0::2] + b[0::2, 1::2] + b[1::2, 0::2] + b[1::2, 1::2]
    u = np.clip(((-38 * r2 - 74 * g2 + 112 * b2 + 512) >> 10) + 128, 0, 255).astype(np.uint8)
    v = np.clip(((112 * r2 - 94 * g2 - 18 * b2 + 512) >> 10) + 128, 0, 255).astype(np.uint8)
    hh, hw = h // 2, w // 2
    if hh % 2 == 0:
        # 偶数行:整块重排即可
        out[h : h + hh // 2, :] = u.reshape(hh // 2, 2 * hw)
        out[h + hh // 2 : h + hh, :] = v.reshape(hh // 2, 2 * hw)
    else:
        # 奇数行(罕见):逐行拼接保正确性
        for i in range(hh):
            out[h + i // 2, (i % 2) * hw : (i % 2 + 1) * hw] = u[i]
            out[h + hh // 2 + i // 2, (i % 2) * hw : (i % 2 + 1) * hw] = v[i]


class CaptureVideoTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, source: FrameSource, fps: int = 30):
        super().__init__()
        self.source = source
        self.fps = max(1, fps)
        self._key_ts = 0.0
        self._frames = 0
        self._force_key = False
        self._yuv: np.ndarray | None = None
        self._reformatter = VideoReformatter()  # 复用 SwsContext,勿每帧重建
        self._err_count = 0        # 距上次日志的捕获异常计数(限流聚合)
        self._last_err_log = 0.0

    def force_keyframe(self) -> None:
        """请求下一帧强制关键帧(IDR),用于客户端 video_lost 恢复。"""
        self._force_key = True

    async def next_timestamp(self):
        """按 self.fps 生成帧时间戳节拍。

        aiortc 1.15 基类用模块常量 VIDEO_PTIME(1/30) 固定 30fps 节拍,
        不读 track 的 fps;高帧率(如 60fps)推流必须覆写,否则帧率被死锁在 30。
        """
        if self.readyState != "live":
            raise MediaStreamError
        if hasattr(self, "_timestamp"):
            self._timestamp += int(90000 / self.fps)
            wait = self._start + (self._timestamp / 90000) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            self._start = time.time()
            self._timestamp = 0
        return self._timestamp, fractions.Fraction(1, 90000)

    async def recv(self):
        try:
            frame = await self.source.get_frame()
        except Exception as exc:
            # 捕获层任何异常都不能穿透:aiortc 的 _run_rtp 协程会因异常终止
            # 且不会自动重启,表现为"连接在、画面永久黑"。此处兜底为静止帧;
            # 日志限流聚合(锁屏等场景异常可每帧发生,逐帧完整堆栈会拖垮 IO)。
            self._err_count += 1
            now = time.monotonic()
            if now - self._last_err_log >= 5.0:
                logger.warning("捕获帧异常(近段 %d 次,兜底静止帧): %s",
                               self._err_count, exc)
                self._err_count = 0
                self._last_err_log = now
            frame = None
        pending: Optional[VideoFrame] = None
        if frame is not None:
            bgra = frame.bgra
            if isinstance(bgra, (bytes, bytearray, memoryview)):
                bgra = np.frombuffer(bgra, np.uint8, count=frame.height * frame.stride)
            m = np.ascontiguousarray(bgra, dtype=np.uint8)
            m = m.reshape(frame.height, frame.stride)[:, : frame.width * 4]
            m = m.reshape(frame.height, frame.width, 4)
            img = VideoFrame.from_ndarray(m, format="bgra")
            pending = self._reformatter.reformat(img, format="yuv420p")
            # 私有拷贝 yuv 数据供静止补帧(reformat 缓冲随帧对象生命周期释放)
            self._yuv = np.asarray(pending.to_ndarray(format="yuv420p")).copy()
        elif self._yuv is not None:
            # 无新帧 → 补帧(仅当已有画面)
            await asyncio.sleep(1.0 / self.fps)
        else:
            await asyncio.sleep(0.02)

        if pending is not None:
            new = pending  # 本帧直接推出,零拷贝
        else:
            if self._yuv is None:
                # 尚未收到任何帧:推一帧黑色画面,避免 track 静默
                w = self.source.width or 640
                h = self.source.height or 480
                self._yuv = np.zeros((h * 3 // 2, w), np.uint8)
            new = VideoFrame.from_ndarray(self._yuv, format="yuv420p")

        pts, time_base = await self.next_timestamp()
        new.pts = pts
        new.time_base = time_base
        # IDR 策略:首帧 / 每 5s / 显式请求(编码器按 key_frame 属性处理)
        self._frames += 1
        now = time.time()
        if self._frames == 1 or self._force_key or (now - self._key_ts) >= 5.0:
            new.key_frame = True
            self._key_ts = now
            self._force_key = False
        return new