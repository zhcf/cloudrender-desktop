"""CloudRender Python SDK(aiortc)。

通用云桌面服务端:桌面捕获 → WebRTC 推流,键鼠注入。协议见 docs/protocol.md。
"""
from . import protocol
from .capture import (CapturedFrame, FrameSource, MssScreenSource,
                      NativeCoreCaptureSource, WindowMssSource)
from .inject import (InputInjector, NativeCoreInjector, WindowsInputInjector)
from .session import PeerSession
from .streamer import CaptureVideoTrack

__version__ = "1.0.0"

__all__ = [
    "protocol",
    "CloudRenderServer",
    "PeerSession",
    "FrameSource",
    "CapturedFrame",
    "MssScreenSource",
    "WindowMssSource",
    "NativeCoreCaptureSource",
    "InputInjector",
    "WindowsInputInjector",
    "NativeCoreInjector",
    "CaptureVideoTrack",
    "__version__",
]


def __getattr__(name: str):
    """惰性暴露入口类:``from cloudrender import CloudRenderServer``。

    server.py 同时是 ``-m cloudrender.server`` 的执行入口;若在包内急切
    导入,runpy 会因模块已入 sys.modules 而告警重复执行,故首次访问时才导入。
    """
    if name == "CloudRenderServer":
        from .server import CloudRenderServer
        return CloudRenderServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")