"""FrameSource 抽象与桌面捕获实现。

- ``MssScreenSource`` / ``WindowMssSource``:纯 Python 路径,抓屏后端可选
  DXGI Desktop Duplication(bettercam,零拷贝,单显示器 auto 优先)或 mss 软抓;
- ``NativeCoreCaptureSource``:ctypes 接入 C++ nativecore(DXGI 零拷贝),检测到 DLL 时优先。
"""
from __future__ import annotations

import abc
import asyncio
import ctypes
import dataclasses
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import wdesktop

logger = logging.getLogger("cloudrender.capture")


@dataclass
class CapturedFrame:
    width: int
    height: int
    stride: int
    bgra: "np.ndarray"   # BGRA32 像素 HxWx4 uint8(行字节 = stride,通常 width*4)
    pts_us: int


class FrameSource(abc.ABC):
    """帧源抽象:云桌面模式下 = 桌面捕获;引擎内嵌模式 = 引擎输出的帧。"""

    @property
    @abc.abstractmethod
    def width(self) -> int: ...

    @property
    @abc.abstractmethod
    def height(self) -> int: ...

    @property
    def origin(self) -> tuple:
        """画面左上角在桌面(虚拟屏)坐标系的位置。

        全屏捕获为 (0, 0);窗口捕获为窗口矩形左上角。服务端注入鼠标时会
        把画面内坐标加此偏移转换为桌面绝对坐标。
        """
        return (0, 0)

    async def start(self) -> None: ...

    @abc.abstractmethod
    async def get_frame(self) -> Optional[CapturedFrame]:
        """阻塞至有新帧(带超时重试);无新帧时可返回 None。"""

    def stop_sync(self) -> None:
        """同步释放捕获资源(默认空实现)。

        含阻塞系统调用(bettercam release 等 COM 调用、nativecore stop)
        的子类须覆盖本方法:会话关闭路径会把它放入工作线程限时执行,
        防止阻塞调用冻死事件循环(历史故障:服务端整体静默失去响应)。
        """

    async def stop(self) -> None:
        self.stop_sync()


# ---------------------------------------------------------------------------
# 抓屏后端:GDI 软抓(mss)/ DXGI Desktop Duplication(bettercam)
# ---------------------------------------------------------------------------

class _ScreenGrabber(abc.ABC):
    """抓屏后端接口:传入 mss 风格 region dict,返回 (h, w, 4) BGRA ndarray。"""

    name = "?"

    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def grab(self, region) -> Optional["np.ndarray"]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 安全桌面(锁屏/UAC)检测:安全桌面激活时,本进程既看不到交互桌面(DXGI
# Duplication 创建被系统拒绝 E_ACCESSDENIED),GDI BitBlt 抓取也全部失败。
# 若不检测并短路,锁屏期间抓屏会在错误路径上反复挣扎:bettercam 重建死循环
# 挂起抓屏线程、mss 每帧抛异常形成日志风暴 —— 最终表现为浏览器端
# "连接断掉/画面卡死"。
# ---------------------------------------------------------------------------

_secure_cache = {"ts": 0.0, "active": False}
_SECURE_CHECK_TTL = 0.5  # 秒;抓屏每帧调用,缓存摊薄系统调用开销


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


_secure_frame_log = {"locked": False, "last": 0.0}


def _note_secure_result(ok: bool) -> None:
    """锁屏帧源状态日志:每次锁屏周期首帧记一条,失败每 30s 重报一条。"""
    now = time.monotonic()
    if not _secure_frame_log["locked"]:
        _secure_frame_log["locked"] = True
        _secure_frame_log["last"] = now
        if ok:
            logger.info("锁屏画面已接入: 安全桌面帧源工作正常(锁屏界面实时可见)")
        else:
            logger.warning("锁屏画面帧源不可用(worker 未连接),暂推静止帧")
    elif not ok and now - _secure_frame_log["last"] >= 30.0:
        _secure_frame_log["last"] = now
        logger.warning("锁屏画面仍不可用: worker 无响应")


def _leave_secure() -> None:
    """解锁后重置锁屏帧源状态(下次锁屏重新记录)。"""
    if _secure_frame_log["locked"]:
        _secure_frame_log["locked"] = False


class _MssGrabber(_ScreenGrabber):
    """mss(GDI BitBlt 逐像素软抓)后端;复用外部传入的 mss 实例(布局元数据共用)。"""

    name = "mss"

    def __init__(self, sct):
        self._sct = sct
        self._err_count = 0        # 距上次日志的抓屏失败计数(限流聚合)
        self._last_err_log = 0.0

    def start(self) -> None: ...

    def grab(self, region) -> Optional["np.ndarray"]:
        if secure_desktop_active():
            # 锁屏/UAC:本进程抓不到安全桌面,改从 SYSTEM worker 取锁屏帧
            # (密码输入界面实时可见);取不到则返回无帧(上层推静止帧)
            frame = wdesktop.grab_secure(region)
            _note_secure_result(frame is not None)
            return frame
        _leave_secure()
        try:
            shot = self._sct.grab(region)
        except Exception as exc:
            self._err_count += 1
            now = time.monotonic()
            if now - self._last_err_log >= 5.0:
                logger.warning("mss 抓屏失败(近段 %d 次,推静止帧): %s",
                               self._err_count, exc)
                self._err_count = 0
                self._last_err_log = now
            return None
        # mss 9.x 提供 bgraw(HxWx4 BGRA ndarray);10.x 已移除该属性,
        # 需从 raw(BGRA bytes)自行构造,两层兼容。
        if getattr(shot, "bgraw", None) is not None:
            return np.ascontiguousarray(shot.bgraw, dtype=np.uint8)
        return np.frombuffer(shot.raw, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)


# 进程内同 output 只允许一个活跃 DXGI 消费者(bettercam 对同 output 重复 create
# 会返回既有单例,两个消费者抢同一实例会丢帧),记录活跃实例:
_dxgi_active: dict = {}

# 占用者僵尸判定:正常抓帧间隔为毫秒级,超过该秒数没有任何 grab 调用
# 视为持有者已死但释放未完成,允许新会话强制回收接管
_DXGI_TAKEOVER_IDLE = 60.0


class _DxgiGrabber(_ScreenGrabber):
    """DXGI Desktop Duplication 后端(bettercam):GPU 侧复制整帧到 staging 纹理后
    映射内存,不做逐像素软件抓屏;同屏抓屏耗时约为 mss 的 1/300。

    - 屏幕无更新时 Duplication 不返回新帧(grab→None),此时复用上一帧
      (region 不变即同一画面,复用正确);
    - 每个输出同时只允许一个 Duplication,且 bettercam 对同 output 重复
      create 会返回既有单例(两消费者抢帧会导致画面停更):本进程内第二个
      会话被显式拒绝,``auto`` 模式自动回退 mss;跨进程由 Duplication 被占自然失败;
    - 单输出无法覆盖多显示器拼接的全虚拟屏,故 ``auto`` 仅单显示器时启用;
    - bettercam 实例被 release 后 duplicator 置 None(半死态),单例 weakref
      缓存未及回收时下次 create 会拿回不可用实例:创建时做健康检查并清缓存重试;
    - 锁屏/解锁等 AccessLost 场景 bettercam 会抛异常(COMError/AttributeError),
      必须在此层捕获并重建 —— 异常穿透到 aiortc 会终止推流协程,解锁后
      也无法恢复(表现为"连接在、画面永久黑");
    - 安全桌面(锁屏/UAC)激活时 DXGI 创建必被拒(E_ACCESSDENIED):检测到
      即短路返回无帧;bettercam 内部 ``_on_output_change`` 的无退避重建
      死循环被替换为限次退避版,杜绝锁屏期间挂起抓屏线程。
    """

    name = "dxgi"

    def __init__(self, output_idx: int = 0):
        self._output_idx = output_idx
        self._cam = None
        self._last = None
        self._last_key = None
        self._last_rebuild = 0.0
        self._err_count = 0        # 距上次日志的抓帧异常计数(限流聚合)
        self._last_err_log = 0.0
        self._last_use = 0.0       # 最近一次 grab 调用时刻(占用者僵尸检测)

    @staticmethod
    def _cam_healthy(cam) -> bool:
        """实例可用性:被 release 过的实例 duplicator 为 None(半死态)。"""
        dup = getattr(cam, "_duplicator", None)
        return dup is not None and getattr(dup, "duplicator", None) is not None

    def _purge_bettercam_cache(self) -> None:
        """把本输出从 bettercam 工厂缓存剔除(weakref 表可能残留半死实例),
        并强制回收,确保下次 create 走全新实例。"""
        try:
            import gc

            import bettercam

            factory = bettercam.__dict__.get("__factory")
            instances = getattr(factory, "_camera_instances", None)
            if instances is not None:
                instances.pop((0, self._output_idx), None)
            gc.collect()
        except Exception:  # bettercam 内部结构变化时静默降级
            pass

    def _create_cam(self):
        """创建 bettercam 实例;半死单例(曾被 release)时清缓存重试,失败返回 None。"""
        import bettercam  # 惰性导入,仅 DXGI 路径需要

        for _ in range(3):
            cam = bettercam.create(output_idx=self._output_idx,
                                   output_color="BGRA")
            if cam is None:
                return None
            if self._cam_healthy(cam):
                self._patch_output_change(cam)
                return cam
            logger.warning("bettercam 返回半释放实例,清理缓存后重试")
            self._purge_bettercam_cache()
        return None

    @staticmethod
    def _patch_output_change(cam) -> None:
        """替换 bettercam 实例的 ``_on_output_change``(锁屏挂起问题的根源)。

        原实现重建 Duplicator 是 ``while True`` + COMError ``continue`` 的
        无退避死循环:安全桌面期间 DuplicateOutput 恒失败(拒绝访问),循环
        永不退出,抓屏调用线程被永久挂起(fps=0 且 CPU 空转,直到解锁)。
        替换版:重建限次(20 次)+ 50ms 退避,安全桌面激活时立即放弃,失败
        抛 RuntimeError 交由上层捕获(复用上一帧/节流重建)。

        注意:本进程只用 grab() 同步路径(未调用 bettercam 的采集线程
        start()),因此替换实例方法即可覆盖全部调用点。
        """

        def _guarded() -> None:
            import comtypes
            from bettercam.core.duplicator import Duplicator

            time.sleep(0.1)  # 等待显示模式切换落定(与原实现一致)
            for attr in ("_duplicator", "_stagesurf"):
                try:
                    getattr(cam, attr).release()
                except Exception:
                    pass
            cam._output.update_desc()
            cam.width, cam.height = cam._output.resolution
            if cam.region is None or not cam._region_set_by_user:
                cam.region = (0, 0, cam.width, cam.height)
            cam._validate_region(cam.region)
            if cam.is_capturing:
                cam._rebuild_frame_buffer(cam.region)
            cam.rotation_angle = cam._output.rotation_angle
            last_exc = None
            for _ in range(20):
                if secure_desktop_active():
                    raise RuntimeError(
                        "secure desktop active (lock screen/UAC); skipping DXGI rebuild")
                try:
                    cam._stagesurf.rebuild(output=cam._output,
                                           device=cam._device)
                    cam._duplicator = Duplicator(output=cam._output,
                                                 device=cam._device)
                    return
                except comtypes.COMError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            raise RuntimeError(
                "DXGI Duplication rebuild failed after retries: %s" % (last_exc,))

        cam._on_output_change = _guarded

    def _release_cam(self) -> None:
        if self._cam is not None:
            try:
                self._cam.release()
            except Exception:
                pass
            self._cam = None

    def start(self) -> None:
        occ = _dxgi_active.get(self._output_idx)
        if occ is not None:
            idle = time.monotonic() - getattr(occ, "_last_use", 0.0)
            if idle > _DXGI_TAKEOVER_IDLE:
                # 占用者已超时无帧请求:其 close 多半卡死(DXGI 占用永不
                # 归还),按僵尸强制回收接管,避免全进程被迫长期 mss 软抓
                logger.warning(
                    "DXGI 占用者 %.0fs 无帧请求(占用未释放),强制回收接管",
                    idle)
                try:
                    occ.close()
                except Exception:
                    logger.warning("强制回收 DXGI 占用者异常", exc_info=True)
                _dxgi_active.pop(self._output_idx, None)
            else:
                raise RuntimeError(
                    f"DXGI output {self._output_idx} is already held by another session in this process")
        cam = self._create_cam()
        if cam is None:
            raise RuntimeError("DXGI Duplication init failed (output missing or exclusively held)")
        self._cam = cam
        self._last_use = time.monotonic()
        _dxgi_active[self._output_idx] = self

    def _try_rebuild(self) -> bool:
        """grab 异常后的重建(节流 1s):释放旧实例→清缓存→重建。

        安全桌面(锁屏/UAC)期间直接放弃:此时重建必被拒(拒绝访问),还会
        留下半初始化实例触发 GC 报错;保持现有实例,待解锁后由首次抓帧
        自然触发重建。
        """
        if secure_desktop_active():
            return False
        now = time.monotonic()
        if now - self._last_rebuild < 1.0:
            return False
        self._last_rebuild = now
        occ = _dxgi_active.get(self._output_idx)
        if occ is not None and occ is not self:
            return False  # 占用已被其他会话接管(本实例曾被按僵尸回收)
        logger.info("重建 DXGI 捕获(bettercam 实例可能已失效)")
        try:
            self._release_cam()
            self._purge_bettercam_cache()
            cam = self._create_cam()
            if cam is None:
                return False
            self._cam = cam
            _dxgi_active[self._output_idx] = self
            self._last = None
            self._last_key = None
            return True
        except Exception:
            logger.exception("DXGI 捕获重建失败")
            return False

    def grab(self, region) -> Optional["np.ndarray"]:
        self._last_use = time.monotonic()  # 心跳:占用者存活标记
        key = (int(region["left"]), int(region["top"]),
               int(region["width"]), int(region["height"]))
        if secure_desktop_active():
            # 锁屏/UAC:改从 SYSTEM worker 取锁屏帧(密码输入界面);
            # 绝不能走 bettercam 的更新/重建路径:其重建在安全桌面期间
            # 无法成功(拒绝访问),只会在错误路径上空转。
            frame = wdesktop.grab_secure(region)
            _note_secure_result(frame is not None)
            return frame
        _leave_secure()
        frame = None
        try:
            frame = self._cam.grab(region=(key[0], key[1],
                                           key[0] + key[2],
                                           key[1] + key[3]))
        except Exception as exc:
            # 锁屏/解锁等 AccessLost 场景 bettercam 可能抛出异常(release_frame
            # 的 COMError 或半死实例的 AttributeError);若穿透到 aiortc,推流
            # 协程会终止且解锁后无法恢复,故在此捕获并尝试重建。日志限流聚合
            # (异常可每帧发生,逐条完整堆栈会拖垮 IO)。
            self._err_count += 1
            now = time.monotonic()
            if now - self._last_err_log >= 5.0:
                logger.warning("DXGI 抓帧异常(近段 %d 次,尝试重建): %s",
                               self._err_count, exc)
                self._err_count = 0
                self._last_err_log = now
            if self._try_rebuild():
                try:
                    frame = self._cam.grab(
                        region=(key[0], key[1], key[0] + key[2],
                                key[1] + key[3]))
                except Exception as exc2:
                    logger.warning("DXGI 重建后抓帧仍失败: %s", exc2)
        if frame is None:
            # 屏幕无更新/本次不可用:复用上一帧(region 相同即同一画面);
            # region 已变则交上层处理
            return self._last if self._last_key == key else None
        self._last = frame
        self._last_key = key
        return frame

    def close(self) -> None:
        if _dxgi_active.get(self._output_idx) is self:
            _dxgi_active.pop(self._output_idx, None)
        self._release_cam()
        self._purge_bettercam_cache()
        self._last = None
        self._last_key = None


def _display_count() -> int:
    """当前活跃显示器数量(SM_CMONITORS;无头且无虚拟显示器时可能为 0/1)。"""
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(80))
    except Exception:
        return 1


def _make_grabber(backend: str, sct) -> _ScreenGrabber:
    """创建并启动抓屏后端。

    backend: ``auto``(单显示器且初始化成功时 DXGI 优先)/ ``dxgi``(强制) /
    ``mss``(强制 GDI 软抓)。
    """
    if backend in ("auto", "dxgi"):
        if backend == "auto" and _display_count() > 1:
            logger.info("多显示器环境:回退 mss 软抓(单输出无法覆盖全虚拟屏)")
        else:
            grabber = _DxgiGrabber()
            try:
                grabber.start()
            except Exception as exc:
                if backend == "dxgi":
                    raise RuntimeError(f"DXGI capture start failed: {exc}") from exc
                logger.warning("DXGI 抓屏不可用(%s),回退 mss 软抓", exc)
            else:
                logger.info("抓屏后端: DXGI Desktop Duplication(零拷贝)")
                return grabber
    grabber = _MssGrabber(sct)
    grabber.start()
    logger.info("抓屏后端: mss 软抓(GDI)")
    return grabber


# ---------------------------------------------------------------------------
# 纯 Python fallback:mss / DXGI
# ---------------------------------------------------------------------------

class MssScreenSource(FrameSource):
    """整屏捕获。抓屏后端可选 DXGI 零拷贝(默认 auto)或 mss 软抓。"""

    def __init__(self, monitor: int = 0, max_fps: int = 30,
                 backend: str = "auto"):
        self._monitor = monitor
        self._max_fps = max_fps
        self._interval = 1.0 / max(1, max_fps)
        self._sct = None
        self._region = None
        self._last_pts = 0.0
        self._backend = backend
        self._grabber: Optional[_ScreenGrabber] = None
        self._last_upgrade_try = 0.0   # mss→DXGI 升级重试节流(monotonic 秒)

    @property
    def width(self) -> int:
        return int(self._region["width"]) if self._region else 0

    @property
    def height(self) -> int:
        return int(self._region["height"]) if self._region else 0

    async def start(self) -> None:
        import mss  # 惰性导入,仅 fallback 路径需要

        self._sct = mss.mss()
        monitors = self._sct.monitors
        if not (0 <= self._monitor < len(monitors)):  # monitors[0] 为全虚拟屏,实际屏从 1 起
            logger.warning("monitor %d 不存在,回退到主显示器", self._monitor)
            self._monitor = 1 if len(monitors) > 1 else 0
            if self._monitor >= len(monitors):
                self._monitor = 0
        self._region = monitors[self._monitor]
        self._grabber = _make_grabber(self._backend, self._sct)
        logger.info("整屏捕获 %dx%d@%s (monitor=%d)", self.width, self.height,
                    self._region, self._monitor)

    async def _throttle(self) -> None:
        """睡到下一个计划抓拍时刻(节拍调度)。

        不要在本层返回 None 让上层再 sleep(两处 sleep 叠加会把帧率拖到 ~12fps)。
        """
        now = time.monotonic()
        if now - self._last_pts < self._interval:
            await asyncio.sleep(self._interval - (now - self._last_pts))
        if self._last_pts <= 0:
            self._last_pts = time.monotonic()
        else:
            self._last_pts += self._interval  # 按计划推进,避免 grab 耗时累积漂移

    async def get_frame(self) -> Optional[CapturedFrame]:
        await self._throttle()
        self._maybe_upgrade_backend()
        grabber = self._grabber
        if grabber is None:   # stop 后仍被驱动(会话收尾期):静默无帧
            return None
        bgra = await asyncio.to_thread(grabber.grab, self._region)
        if bgra is None:
            return None
        h, w = int(bgra.shape[0]), int(bgra.shape[1])
        return CapturedFrame(width=w, height=h, stride=w * 4, bgra=bgra,
                             pts_us=int(time.time() * 1e6))

    def _maybe_upgrade_backend(self) -> None:
        """mss 运行期周期(30s)尝试升级 DXGI:占用释放后自动提速。

        会话建立时 DXGI 若被其他会话占用会回退 mss 软抓(约 39fps 上限),
        占用释放后原会话不会自动重试,须在本层周期抢占升级;仅 auto 模式
        生效(显式 backend=mss/dxgi 为使用者意图,不做迁移),多显示器与
        安全桌面期间跳过。
        """
        if self._backend != "auto":
            return
        if not isinstance(self._grabber, _MssGrabber):
            return
        now = time.monotonic()
        if now - self._last_upgrade_try < 30.0:
            return
        self._last_upgrade_try = now
        if _display_count() > 1 or secure_desktop_active():
            return
        grabber = _DxgiGrabber()
        try:
            grabber.start()
        except Exception:
            return  # 仍被占用/初始化失败:保持 mss,下轮再试
        logger.info("抓屏后端升级: mss 软抓 → DXGI Desktop Duplication")
        old = self._grabber
        self._grabber = grabber
        try:
            old.close()
        except Exception:
            pass

    def stop_sync(self) -> None:
        if self._grabber is not None:
            try:
                self._grabber.close()
            except Exception:
                pass
            self._grabber = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # mss 无 close 的旧版本
                pass
            self._sct = None

    async def stop(self) -> None:
        self.stop_sync()


# ---------------------------------------------------------------------------
# 窗口裁剪 helper(ctypes,仅 Windows)
# ---------------------------------------------------------------------------

_dpi_aware = False


def _even(v: int) -> int:
    """向下对齐到偶数(yuv420p 色度下采样要求)。"""
    return (int(v) // 2) * 2


def _enable_dpi_awareness() -> None:
    """进程级 DPI 感知:GetWindowRect/GetMonitorInfoW 才返回物理像素(与 mss 同坐标系)。"""
    global _dpi_aware
    if _dpi_aware:
        return
    _dpi_aware = True
    try:
        # 每显示器感知 v2(Win10 1703+);失败回退系统级感知(Vista+)
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except OSError:
            logger.warning("设置 DPI 感知失败,窗口裁剪在高 DPI 屏可能错位")


def _window_rect(hwnd: int):
    """窗口可视矩形(虚拟屏物理像素坐标)。最小化/已关闭返回 None。"""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return (int(rect.left), int(rect.top), int(w), int(h))


def _find_window_by_title(title: str):
    """按标题子串查找窗口(不区分大小写),返回第一个;不存在返回 None。

    用于画布模式:目标窗口关闭后自动重查新窗口句柄,窗口重开即恢复出画。
    """
    from ctypes import wintypes

    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def _window_monitor_region(hwnd: int):
    """窗口所在显示器物理矩形(rcMonitor),转为 mss 可用的 region dict;失败返回 None。"""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon:
        return None

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return {"left": int(r.left), "top": int(r.top),
            "width": int(r.right - r.left), "height": int(r.bottom - r.top)}


class WindowMssSource(MssScreenSource):
    """纯 Python 窗口捕获:mss 抓窗所在显示器 + 窗口矩形动态裁剪(免 nativecore)。

    与 nativecore v1 窗口裁剪模式语义一致:

    - 每帧动态取窗口矩形,移动/缩放自动跟随,跨屏自动切换抓取区域;
    - 窗口被遮挡时裁剪区显示遮挡内容(OS 限制);
    - 最小化/关闭时返回 None,由上层(streamer)推静止帧;
    - 尺寸向下对齐到偶数(yuv420p 要求),奇数尺寸窗口右边/底边少 1 像素。

    画布模式 (``canvas=True``):帧尺寸固定为全虚拟屏(黑底),仅窗口区域贴
    真实内容,其余全黑;窗口消失(关闭/最小化)时整帧全黑(不再是静止残影);
    提供 ``window_title`` 时窗口关闭后每秒钟自动重查新句柄,重开即恢复出画。
    画布模式始终推帧(永不返回 None),视频分辨率恒定。
    """

    def __init__(self, window_hwnd: int, max_fps: int = 30,
                 canvas: bool = False, window_title: str = "",
                 backend: str = "auto"):
        super().__init__(monitor=0, max_fps=max_fps,  # monitor 无效,区域动态决定
                         backend=backend)
        self._window_hwnd = int(window_hwnd)
        self._canvas = bool(canvas)
        self._window_title = str(window_title)
        self._canvas_w = 0
        self._canvas_h = 0
        self._win_w = 0
        self._win_h = 0
        self._origin = (0, 0)
        self._win_rect = None
        self._last_refind = 0.0

    @property
    def width(self) -> int:
        return self._canvas_w if self._canvas else self._win_w

    @property
    def height(self) -> int:
        return self._canvas_h if self._canvas else self._win_h

    @property
    def origin(self) -> tuple:
        """窗口左上角桌面坐标,注入输入时叠加(每帧捕获时更新);
        画布模式为虚拟屏原点(内容即全屏画面,坐标直接对应桌面)。"""
        return self._origin

    @property
    def input_active(self) -> bool:
        """是否允许注入输入。画布模式下窗口消失(关闭/最小化)时为 False,
        session 据此丢弃输入,避免黑屏时误操作桌面其它应用。"""
        if not self._canvas:
            return True
        return self._window_hwnd and _window_rect(self._window_hwnd) is not None

    @property
    def window_rect(self) -> Optional[tuple]:
        """画布模式下目标窗口的桌面矩形 (x, y, w, h, 虚拟屏坐标),
        随每帧捕获刷新;窗口不可见时为 None。非画布模式恒为 None
        (调用方按不裁剪处理,保持原行为)。"""
        return self._win_rect if self._canvas else None

    async def start(self) -> None:
        import mss  # 惰性导入,仅 fallback 路径需要

        _enable_dpi_awareness()
        self._sct = mss.mss()
        self._full = self._sct.monitors[0]  # 全虚拟屏,窗口跨屏时兜底
        self._grabber = _make_grabber(self._backend, self._sct)
        if self._canvas:
            self._canvas_w = _even(self._full["width"])
            self._canvas_h = _even(self._full["height"])
            self._region = self._full
            if self._window_hwnd and not ctypes.windll.user32.IsWindow(self._window_hwnd):
                self._window_hwnd = 0  # 启动时窗口已关闭:按标题每帧重查
            if not self._window_hwnd and self._window_title:
                self._window_hwnd = _find_window_by_title(self._window_title) or 0
            logger.info("画布捕获 %dx%d hwnd=0x%x title=%r",
                        self._canvas_w, self._canvas_h, self._window_hwnd,
                        self._window_title)
            return
        if not ctypes.windll.user32.IsWindow(self._window_hwnd):
            raise RuntimeError("window handle is stale (hwnd=0x%x)" % self._window_hwnd)
        rect = _window_rect(self._window_hwnd)
        if rect is None:
            # 窗口最小化:允许会话建立,窗口恢复后自动出画(期间推黑色帧)
            logger.warning("窗口 %#x 当前最小化,恢复后自动出画", self._window_hwnd)
        else:
            self._win_w, self._win_h = _even(rect[2]), _even(rect[3])
        self._region = _window_monitor_region(self._window_hwnd) or self._full
        logger.info("窗口捕获 hwnd=0x%x 窗口 %dx%d (region=%s)",
                    self._window_hwnd, self._win_w, self._win_h,
                    (self._region["width"], self._region["height"]))

    async def get_frame(self) -> Optional[CapturedFrame]:
        await self._throttle()
        result = await asyncio.to_thread(self._grab_window_frame)
        if result is None:
            return None
        bgra, w, h = result
        return CapturedFrame(width=w, height=h, stride=w * 4, bgra=bgra,
                             pts_us=int(time.time() * 1e6))

    def _grab_window_frame(self):
        """同步线程内:窗口矩形 → 定位显示器 → 抓屏 → 裁剪。无帧可推时返回 None。"""
        if self._canvas:
            return self._grab_canvas_frame()
        rect = _window_rect(self._window_hwnd)
        if rect is None:
            return None
        x, y, w, h = rect
        region = _window_monitor_region(self._window_hwnd)
        if region is None:
            region = self._full  # 跨屏组合窗口:抓全虚拟屏裁剪
        self._region = region
        try:
            full = self._grabber.grab(region)
        except Exception:
            logger.exception("窗口抓屏失败")
            return None
        if full is None:
            return None  # 暂无可用帧(DXGI 首帧未就绪等)
        fh, fw = int(full.shape[0]), int(full.shape[1])
        # 虚拟屏坐标 → 相对抓取区域坐标,并裁剪交集
        rx, ry = region["left"], region["top"]
        x0 = max(x - rx, 0)
        y0 = max(y - ry, 0)
        x1 = min(x - rx + w, fw)
        y1 = min(y - ry + h, fh)
        if x1 <= x0 or y1 <= y0:
            return None  # 窗口完全移出抓取区域
        # 画面左上角对应的桌面坐标(窗口部分越出屏幕时跟随裁剪起点)
        self._origin = (rx + x0, ry + y0)
        # yuv420p 色度 2:1 下采样要求宽高为偶数;窗口尺寸可能为奇数(如 827),
        # 裁剪区向下对齐到偶数,编码层(av.to_ndarray)才不会断言失败
        x1 = x0 + ((x1 - x0) // 2) * 2
        y1 = y0 + ((y1 - y0) // 2) * 2
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None  # 裁剪区不足 2 像素(极端场景)
        self._win_w, self._win_h = x1 - x0, y1 - y0
        return np.ascontiguousarray(full[y0:y1, x0:x1]), x1 - x0, y1 - y0

    def _grab_canvas_frame(self):
        """画布模式:整帧全虚拟屏尺寸,黑底;仅窗口区域贴内容;窗口不在时整帧黑。

        每秒最多重查一次窗口标题,窗口关闭后重开自动出画。
        """
        region = self._full
        try:
            full = self._grabber.grab(region)
        except Exception:
            logger.exception("画布抓屏失败")
            return None
        if full is None:
            return None  # 首帧尚未就绪(极短窗口期),由 streamer 兜底黑帧
        rx, ry = region["left"], region["top"]
        self._origin = (rx, ry)  # 整帧即全屏画面,注入坐标 = 虚拟屏坐标

        hwnd = self._window_hwnd
        rect = _window_rect(hwnd)
        if rect is None and self._window_title:
            now = time.monotonic()
            if now - self._last_refind >= 1.0:
                self._last_refind = now
                new_hwnd = _find_window_by_title(self._window_title)
                if new_hwnd:
                    self._window_hwnd = hwnd = new_hwnd
                    logger.info("画布模式:重新找到窗口 hwnd=0x%x", new_hwnd)
                    rect = _window_rect(hwnd)

        # 整帧黑底 + 窗口区域粘内容;yuv420p 要求尺寸偶数(画布启动时已对齐)
        canvas = np.zeros((self._canvas_h, self._canvas_w, 4), dtype=np.uint8)
        if rect is not None:
            x, y, w, h = rect
            x0 = max(x - rx, 0)
            y0 = max(y - ry, 0)
            x1 = min(x - rx + w, self._canvas_w)
            y1 = min(y - ry + h, self._canvas_h)
            if x1 > x0 and y1 > y0:
                canvas[y0:y1, x0:x1] = full[y0:y1, x0:x1]
        # 记录窗口桌面矩形(供 session 做输入裁剪,黑屏区点击不注入桌面其它应用)
        self._win_rect = rect
        return np.ascontiguousarray(canvas), self._canvas_w, self._canvas_h


# ---------------------------------------------------------------------------
# nativecore ctypes 桥
# ---------------------------------------------------------------------------

class _CrFrame(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("stride", ctypes.c_uint32),
        ("pts_us", ctypes.c_uint64),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("is_new", ctypes.c_uint8),
    ]


class _CrWindowInfo(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_uint64),
        ("title", ctypes.c_char * 256),
    ]


def find_nativecore() -> Optional[ctypes.CDLL]:
    """按序查找 nativecore.dll:同目录 → 常见构建输出目录 → PATH。"""
    import glob
    import sys

    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller 冻结模式:add-binary 打入包目录与资源根两处
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidates.append(os.path.join(base, "cloudrender", "nativecore.dll"))
        candidates.append(os.path.join(base, "nativecore.dll"))
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, "..", "..", ".."))
    candidates.append(os.path.join(here, "nativecore.dll"))
    candidates += sorted(glob.glob(
        os.path.join(repo, "server", "cpp", "nativecore", "build", "**",
                     "nativecore.dll"),
        recursive=True), reverse=True)
    candidates.append("nativecore.dll")
    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


# 保存捕获循环线程引用,防止 Python 端 GC 期间回调崩溃
_keepalive: list = []


class NativeCoreCaptureSource(FrameSource):
    """DXGI Desktop Duplication 捕获(nativecore)。回调线程 → 队列 → asyncio。"""

    def __init__(self, monitor: int = 0, window_hwnd: int = 0, max_fps: int = 30):
        self._monitor = monitor
        self._window_hwnd = window_hwnd
        self._origin = (0, 0)
        self._max_fps = max_fps
        self._lib: Optional[ctypes.CDLL] = None
        self._cap = None
        self._cb = None
        self._queue: "queue.Queue[Optional[CapturedFrame]]" = queue.Queue(maxsize=4)
        self._frame: Optional[CapturedFrame] = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._lib is not None

    @property
    def width(self) -> int:
        return self._frame.width if self._frame else 0

    @property
    def height(self) -> int:
        return self._frame.height if self._frame else 0

    @property
    def origin(self) -> tuple:
        """窗口捕获时代入窗口左上角桌面坐标,全屏捕获为 (0, 0)。"""
        if self._window_hwnd:
            rect = _window_rect(self._window_hwnd)
            if rect is not None:
                return (rect[0], rect[1])
        return self._origin

    def _proto(self, lib) -> None:
        lib.cr_capture_create.restype = ctypes.c_void_p
        lib.cr_capture_destroy.argtypes = [ctypes.c_void_p]
        lib.cr_capture_set_frame_callback.argtypes = [
            ctypes.c_void_p, ctypes.CFUNCTYPE(None, ctypes.c_void_p,
                                              ctypes.POINTER(_CrFrame)),
            ctypes.c_void_p]
        lib.cr_capture_set_max_fps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.cr_capture_set_target.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint64]
        lib.cr_capture_start.argtypes = [ctypes.c_void_p]
        lib.cr_capture_stop.argtypes = [ctypes.c_void_p]

    async def start(self) -> None:
        self._lib = find_nativecore()
        if self._lib is None:
            raise RuntimeError("nativecore.dll not found (use MssScreenSource instead)")
        self._proto(self._lib)
        _keepalive.append(self)  # 防止 nativecore 库对象被回收

        def on_frame(_user, frame_ptr):
            frame = frame_ptr.contents
            size = frame.height * frame.stride
            if size <= 0:
                return
            buf = ctypes.string_at(frame.data, size)
            item = CapturedFrame(width=frame.width, height=frame.height,
                                 stride=frame.stride, bgra=buf, pts_us=frame.pts_us)
            with self._lock:
                self._frame = item
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(item)
                except queue.Empty:
                    pass

        self._cb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_CrFrame))(on_frame)
        self._cap = self._lib.cr_capture_create()
        if not self._cap:
            raise RuntimeError("cr_capture_create failed")
        if self._lib.cr_capture_set_frame_callback(self._cap, self._cb, None) != 0:
            raise RuntimeError("failed to register frame callback")
        self._lib.cr_capture_set_max_fps(self._cap, self._max_fps)
        monitor = self._monitor
        hwnd = self._window_hwnd
        if hwnd:
            monitor = -1  # 由 nativecore 按窗口解析显示器
        if self._lib.cr_capture_set_target(self._cap, monitor, hwnd) != 0:
            raise RuntimeError("capture start failed (monitor in use or invalid parameters?)")
        if self._lib.cr_capture_start(self._cap) != 0:
            raise RuntimeError("capture thread failed to start")
        logger.info("nativecore 捕获已启动 (monitor=%d hwnd=0x%x)", self._monitor, hwnd)

    async def get_frame(self) -> Optional[CapturedFrame]:
        return await asyncio.to_thread(self._queue.get, True, 2.0)  # 2s 超时返回 None

    def stop_sync(self) -> None:
        if self._cap:
            self._lib.cr_capture_stop(self._cap)
            self._lib.cr_capture_destroy(self._cap)
            self._cap = None
        if self in _keepalive:
            _keepalive.remove(self)
        # 清空队列
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass


def load_nativecore_or_none() -> Optional[ctypes.CDLL]:
    return find_nativecore()