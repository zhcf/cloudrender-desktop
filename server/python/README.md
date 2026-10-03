# CloudRender Python SDK(服务端)

基于 aiortc 的通用云桌面服务端 SDK:捕获本机桌面 → WebRTC 推送浏览器 → 键鼠反向注入。协议唯一实现见 `cloudrender/protocol.py`。

## 安装

```bash
pip install -r requirements.txt     # aiohttp + aiortc + numpy + mss
```

## 快速开始(本机云桌面 Demo)

```bash
cd server/python
python -m cloudrender.server --port 8080
# 浏览器打开 http://localhost:8080/(服务端内置 Web UI,页面自动连接同端口信令)
```

> 入口必须以 `-m` 方式运行(包内全部为相对导入);`python cloudrender\server.py` 直跑不支持。

服务端默认监听 0.0.0.0:8080;同一局域网另一台电脑浏览器打开 `http://<本机局域网IP>:8080/`
(ipconfig 查 IPv4,如 192.168.1.105)即用(页面与信令同端口);若连不上,
检查本机 Windows 防火墙是否放行 8080 入站。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | 0.0.0.0 | 监听地址 |
| `--port` | 8080 | 信令与 Web UI 端口 |
| `--monitor` | 0 | 0=整个桌面(全虚拟屏),1=主显示器,2..=其余显示器 |
| `--fps` | 30 | 帧率上限 |
| `--bitrate` | 6000 | 视频目标码率 kbps;过低画面发糊,局域网可上调至 8000-12000 |
| `--token` | 无 | 可选鉴权(`ws?token=...`) |
| `--max-sessions` | 1 | 并发会话上限(nativecore 抓屏同显示器互斥) |
| `--capture` | auto | auto=单显示器优先 DXGI 零拷贝,失败/多屏回退 mss;mss=强制 GDI 软抓;dxgi=强制 DXGI |
| `--force-fallback` | - | 等价 `--capture mss`(不使用 nativecore/DXGI) |

无头(无物理显示器)运行:需先安装/启用虚拟显示器驱动(IddCx 类,分辨率设 1920x1080),启动时会检测显示器数量并提示。

## 包结构与分层(`cloudrender/`)

包内模块分两层——**SDK 层**(可独立集成的会话/捕获能力)与**应用层**(服务端进程自身),同属一个自包含单元,不物理拆分;与 C++ 侧 `cpp/sdk` + `cpp/shell` 的映射见下。

```
cloudrender/
├── server.py           # CLI 入口 + CloudRenderServer(aiohttp 信令 + 会话编排,合并一体)
├── protocol.py         # 协议唯一实现:信令/输入/事件帧编解码、FNV 键码表
├── capture.py          # FrameSource 抽象;MssScreenSource / NativeCoreCaptureSource(ctypes)
├── inject.py           # InputInjector 抽象;WindowsInputInjector(SendInput) / NativeCoreInjector
├── session.py          # PeerSession:aiortc 媒体会话(offer/answer/ice、数据通道)
├── streamer.py         # CaptureVideoTrack:帧泵(拉最新帧 → 编码推送)
├── webui.py            # 内置 Web UI:把 client/web 与 client/javascript/src 挂进 aiohttp
├── wdesktop.py         # 安全桌面(锁屏画面/输入)worker 的本地客户端
├── wdesktop_worker.py  # 安全桌面 worker 本体(以 winlogon 令牌 spawn,SYSTEM)
├── winlogon.py         # spawn_wdesktop_worker:提权 spawn(需管理员,SeDebugPrivilege)
├── mini_client.py      # 无 GUI 协议验证客户端(解码帧统计 fps + 命令行注入)
└── __init__.py         # 包导出(CloudRenderServer 惰性导出,供编程式导入)
```

| 层 | 模块 | C++ 对位 |
|---|---|---|
| SDK 层 | `protocol.py` | `cpp/sdk/wire/cr_wire.*`(字节级一致,契约测试保证) |
| SDK 层 | `capture.py` / `inject.py` | `cpp/sdk/session/`:`cr_source.hpp` + `nativecore_source.*`(nativecore 适配) |
| SDK 层 | `session.py` / `streamer.py` | `cpp/sdk/session/cr_webrtc_session.*`(libwebrtc 会话 + 帧泵) |
| 应用层 | `server.py` | `cpp/shell/cpp_server.py`(同职责信令壳,媒体核心换 cloudrender_session.dll) |
| 应用层 | `webui.py` / `wdesktop.py` / `winlogon.py` / `wdesktop_worker.py` | `cpp/shell/` 同名副本(同源同步) |
| 应用层 | `mini_client.py` | —(仅 Python 侧) |

> C++ 侧另有 C ABI 出口层(`cpp/sdk/capi/cr_session_api.h`,8081 壳经 ctypes 加载);Python 无此层——同进程直接 import。
> `cpp/shell/` 内的 protocol/webui/wdesktop/winlogon/wdesktop_worker 五个模块均为本包同源副本,修改须两处同步。

## 编程式用法

```python
from cloudrender import CloudRenderServer, MssScreenSource, WindowsInputInjector

server = CloudRenderServer(port=8080, token=None, max_sessions=1, bitrate=8_000_000)
server.run(
    source_factory=lambda info: MssScreenSource(monitor=0, max_fps=30),
    injector_factory=lambda info: WindowsInputInjector(),
)
```

云桌面模式下把 `MssScreenSource` 换成 `NativeCoreCaptureSource`(nativecore.dll 在 `server/cpp/nativecore/build/Release/`)即获 DXGI 零拷贝捕获;引擎内嵌模式实现自己的 `FrameSource` 即可(见 docs/integration.md)。

## 联调验证(mini_client)

```bash
python -m cloudrender.mini_client ws://127.0.0.1:8080/ws
# 命令:move <x> <y> | click <button> | key <code> | text <str> | quit
```

## 锁屏画面(安全桌面)

锁屏期间普通进程无法抓屏/注入,服务端会以 winlogon 令牌 spawn SYSTEM worker(`wdesktop_worker.py`)接管,浏览器可直接输入锁屏密码。
**需以管理员权限启动服务端**(SeDebugPrivilege);否则降级为锁屏静帧。worker 日志见 `server/python/_wdesktop_worker.log`。