"""桌面系统音频采集(WASAPI loopback)→ aiortc 音频轨。

与 C++ 会话核心(cr_audio_capture.cpp)行为一致:

- 采集默认播放设备(eRender/eConsole)的混音,不占用麦克风;
- 共享模式 loopback;混音格式可能是 16/24/32-bit PCM 或 32-bit float,
  统一转 16-bit 交错立体声(C++ 侧同样保留 L/R 前两声道,单声道复制);
- 设备/格式采样率非 48kHz 时用 PyAV AudioResampler 重采样;
- 固定 20ms(960 样本)分块交给 aiortc(Opus 编码帧长);
- 设备切换/拔出/独占被抢时,采集线程每秒重试直至停止;
- 音频轨在 offer 中常驻,开关只启停采集,不触发 SDP 重协商。

COM 接口用手写 vtable 定义(comtypes),无需额外依赖或类型库注册。
"""
from __future__ import annotations

import asyncio
import ctypes
import fractions
import logging
import queue
import sys
import threading
import time
from ctypes import (POINTER, byref, c_int, c_longlong, c_ulong, c_ushort,
                    c_void_p)
from typing import Optional

import av
import numpy as np
from aiortc import MediaStreamError
from aiortc.mediastreams import AudioStreamTrack

logger = logging.getLogger("cloudrender.audio")

SAMPLE_RATE = 48000          # Opus 编码采样率
CHANNELS = 2                 # 统一输出立体声
CHUNK_MS = 20                # WebRTC 音频帧节奏
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000
CHUNK_BYTES = CHUNK_SAMPLES * CHANNELS * 2

CLSCTX_ALL = 23
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x0002
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_IS_WINDOWS = sys.platform == "win32"
_ole32 = ctypes.windll.ole32 if _IS_WINDOWS else None
if _ole32 is not None:
    _ole32.CoTaskMemFree.argtypes = [c_void_p]
    _ole32.CoTaskMemFree.restype = None


# ---------------------------------------------------------------------------
# WASAPI COM 接口(comtypes 手写定义;方法顺序必须与 SDK 头一致)
# ---------------------------------------------------------------------------

from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402

KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00AA00389B71}")
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID(
    "{00000003-0000-0010-8000-00AA00389B71}")


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", c_ulong),
        ("nAvgBytesPerSec", c_ulong),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("Samples", c_ushort),
        ("dwChannelMask", c_ulong),
        ("SubFormat", GUID),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], c_ulong, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore",
                  (["in"], c_ulong, "stgmAccess"),
                  (["out"], POINTER(c_void_p), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId",
                  (["out"], POINTER(c_void_p), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState",
                  (["out"], POINTER(c_ulong), "pdwState")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_ulong, "dwStateMask"),
                  (["out"], POINTER(c_void_p), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice",
                  (["in"], c_void_p, "pwstrId"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                  (["in"], c_void_p, "pClient")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                  (["in"], c_void_p, "pClient")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], c_ulong, "StreamFlags"),
                  (["in"], c_longlong, "hnsBufferDuration"),
                  (["in"], c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize",
                  (["out"], POINTER(c_ulong), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency",
                  (["out"], POINTER(c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding",
                  (["out"], POINTER(c_ulong), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_int, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(c_void_p), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle",
                  (["in"], c_void_p, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(c_void_p), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(c_void_p), "ppData"),
                  (["out"], POINTER(c_ulong), "pNumFramesToRead"),
                  (["out"], POINTER(c_ulong), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_ulonglong), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_ulonglong), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], c_ulong, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(c_ulong), "pNumFramesInNextPacket")),
    ]


# ---------------------------------------------------------------------------
# 混音格式 → 16-bit 立体声
# ---------------------------------------------------------------------------

def _is_float_format(fmt_ptr) -> bool:
    """WAVEFORMATEX/WAVEFORMATEXTENSIBLE 的实际采样格式是否为 float32。"""
    fmt = fmt_ptr.contents
    if fmt.wFormatTag == WAVE_FORMAT_IEEE_FLOAT:
        return True
    if fmt.wFormatTag != WAVE_FORMAT_EXTENSIBLE or fmt.cbSize < 22:
        return False
    ext = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEXTENSIBLE)).contents
    return bool(ext.SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)


def _to_stereo_s16(data_ptr, frames: int, fmt_ptr) -> np.ndarray:
    """把一次 WASAPI 包转成 (frames, 2) int16;不支持/静音返回全 0。"""
    fmt = fmt_ptr.contents
    channels = max(1, int(fmt.nChannels))
    bits = int(fmt.wBitsPerSample)
    block = max(1, int(fmt.nBlockAlign))
    if data_ptr is None or frames <= 0 or block < channels * max(1, bits // 8):
        return np.zeros((max(0, frames), CHANNELS), np.int16)
    raw = ctypes.string_at(data_ptr, frames * block)
    rows = np.frombuffer(raw, np.uint8).reshape(frames, block)
    if bits == 16:
        arr = np.ascontiguousarray(rows[:, :channels * 2]).view(np.int16)
    elif bits == 24:
        q = np.ascontiguousarray(rows[:, :channels * 3]).reshape(
            frames, channels, 3).astype(np.int32)
        v = q[:, :, 0] | (q[:, :, 1] << 8) | (q[:, :, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)   # 24-bit 符号扩展
        arr = (v >> 8).astype(np.int16)
    elif bits == 32 and _is_float_format(fmt_ptr):
        arr = (np.clip(np.ascontiguousarray(
            rows[:, :channels * 4]).view(np.float32), -1.0, 1.0)
            * 32767.0).astype(np.int16)
    elif bits == 32:
        arr = (np.ascontiguousarray(
            rows[:, :channels * 4]).view(np.int32) >> 16).astype(np.int16)
    else:  # 不支持的位深:静音(共享模式混音格式通常为 float32)
        arr = np.zeros((frames, channels), np.int16)
    if arr.shape[1] == 1:
        return np.repeat(arr, 2, axis=1)
    return np.ascontiguousarray(arr[:, :2])


# ---------------------------------------------------------------------------
# 采集线程
# ---------------------------------------------------------------------------

class LoopbackCapture:
    """WASAPI loopback 采集器:read() 返回固定 20ms/48kHz/s16 立体声 PCM。"""

    def __init__(self):
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._running = False
        self._last_error = ""

    @property
    def running(self) -> bool:
        return self._running and not self._stop.is_set()

    def start(self, timeout: float = 2.0) -> bool:
        """启动采集并等待首包就绪;设备不可用返回 False(后台仍会重试)。"""
        if not _IS_WINDOWS:
            self._last_error = "WASAPI loopback is only available on Windows"
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._started.clear()
            self._drain_queue()
            self._pending.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="cloudrender-audio", daemon=True)
            self._thread.start()
        return self._started.wait(timeout)

    def stop(self) -> None:
        """停止采集并等待线程退出(限时;卡住时保留线程引用防重复启动)。"""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        with self._lock:
            if self._thread is t and (t is None or not t.is_alive()):
                self._thread = None
        self._drain_queue()
        self._pending.clear()

    def read(self, timeout: float = 0.05) -> Optional[bytes]:
        """取一帧 20ms PCM;超时返回 None(调用方补静音)。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- 内部 ---------------------------------------------------------
    def _drain_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _push_pcm(self, pcm: bytes) -> None:
        self._pending += pcm
        while len(self._pending) >= CHUNK_BYTES:
            chunk = bytes(self._pending[:CHUNK_BYTES])
            del self._pending[:CHUNK_BYTES]
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                # 消费端卡顿时丢弃最旧帧,避免延迟累积
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(chunk)
                except queue.Full:
                    pass

    def _thread_main(self) -> None:
        com_ok = False
        try:
            import comtypes
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                com_ok = True
            except OSError:
                # 线程已初始化过 COM(非本线程通常不会发生):仍可尝试采集
                pass
            failures = 0
            while not self._stop.is_set():
                try:
                    done = self._capture_session()
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning("桌面音频采集异常(将重试): %s", exc)
                    done = False
                if done or self._stop.is_set():
                    break
                failures += 1
                if failures % 30 == 1:
                    logger.warning("桌面音频采集不可用(第 %d 次重试): %s",
                                   failures, self._last_error or "未知原因")
                self._stop.wait(1.0)   # 设备切换/独占释放后自动恢复
        finally:
            self._running = False
            self._started.clear()
            if com_ok:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass
            logger.info("桌面音频采集已停止")

    def _capture_session(self) -> bool:
        """建立一次 WASAPI loopback 会话;正常停止返回 True。"""
        import comtypes

        enumerator = comtypes.CoCreateInstance(
            GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            IMMDeviceEnumerator, CLSCTX_ALL)
        device = enumerator.GetDefaultAudioEndpoint(0, 0)  # eRender, eConsole
        iid_client = IAudioClient._iid_
        client = ctypes.cast(device.Activate(byref(iid_client), CLSCTX_ALL, None),
                             POINTER(IAudioClient))
        fmt_ptr = client.GetMixFormat()
        try:
            fmt = fmt_ptr.contents
            rate = int(fmt.nSamplesPerSec)
            client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                              AUDCLNT_STREAMFLAGS_LOOPBACK, 0, 0,
                              fmt_ptr, None)
            iid_cap = IAudioCaptureClient._iid_
            capture = ctypes.cast(client.GetService(byref(iid_cap)),
                                  POINTER(IAudioCaptureClient))
            client.Start()
            resampler = None
            if rate != SAMPLE_RATE:
                resampler = av.AudioResampler(format="s16", layout="stereo",
                                              rate=SAMPLE_RATE)
            self._running = True
            self._last_error = ""
            self._started.set()
            logger.info("桌面音频采集已启动: %d Hz, %d ch (loopback)",
                        rate, int(fmt.nChannels))
            try:
                return self._pump(capture, fmt_ptr, rate, resampler)
            finally:
                try:
                    client.Stop()
                except Exception:
                    pass
        finally:
            _ole32.CoTaskMemFree(ctypes.cast(fmt_ptr, c_void_p))

    def _pump(self, capture, fmt_ptr, rate: int, resampler) -> bool:
        """读取音频包并投递;返回 True 表示按停止请求正常退出。"""
        frames_seen = 0
        while not self._stop.is_set():
            packet = capture.GetNextPacketSize()
            if not packet:
                # 静音时引擎不一定出包:短睡避免空转
                self._stop.wait(0.005)
                continue
            while packet and not self._stop.is_set():
                data, frames, flags, _dev, _qpc = capture.GetBuffer()
                if frames:
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT or not data:
                        stereo = np.zeros((frames, CHANNELS), np.int16)
                    else:
                        stereo = _to_stereo_s16(data, frames, fmt_ptr)
                    self._deliver(stereo, rate, resampler)
                    frames_seen += frames
                capture.ReleaseBuffer(frames)
                packet = capture.GetNextPacketSize()
        if frames_seen == 0:
            logger.warning("桌面音频采集未收到任何数据(默认播放设备可能无输出)")
        return True

    def _deliver(self, stereo: np.ndarray, rate: int, resampler) -> None:
        if resampler is None:
            self._push_pcm(stereo.tobytes())
            return
        frame = av.AudioFrame.from_ndarray(
            stereo.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = rate
        for out in resampler.resample(frame):
            self._push_pcm(out.to_ndarray().tobytes())


# ---------------------------------------------------------------------------
# aiortc 音频轨
# ---------------------------------------------------------------------------

class DesktopAudioTrack(AudioStreamTrack):
    """桌面系统音频轨(常驻 offer;关闭时输出静音,打开后推采集数据)。"""

    kind = "audio"

    def __init__(self, capture: LoopbackCapture):
        super().__init__()
        self._capture = capture
        self._timestamp = 0
        self._start: Optional[float] = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.time()
        if self._start is None:
            self._start = now
            self._timestamp = 0
        else:
            self._timestamp += CHUNK_SAMPLES
            wait = self._start + self._timestamp / SAMPLE_RATE - now
            if wait > 0:
                await asyncio.sleep(wait)
        data = None
        if self._capture.running:
            data = await asyncio.to_thread(self._capture.read, 0.05)
        frame = av.AudioFrame("s16", "stereo", CHUNK_SAMPLES)
        if data is not None and len(data) == CHUNK_BYTES:
            frame.planes[0].update(data)
        else:
            frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        return frame