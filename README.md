# CloudRender 云桌面

基于 **WebRTC** 的云桌面:服务端捕获 OS 桌面/窗口画面(类似桌面截图,所有软件零改造可用),经 WebRTC 推送到浏览器,客户端键鼠反向操作——"本地操作、云端渲染"。

除开箱即用的云桌面 Demo 外,项目同时提供各语言 SDK(服务端 Python/C++、客户端 JavaScript),便于把云桌面能力集成进自有应用。

## 核心能力

- **通用捕获**:渲染源 = 桌面/窗口截屏(DXGI Desktop Duplication 零拷贝),任何软件无需改造;
- **统一协议**:各语言服务端共享同一份信令与输入二进制协议([docs/protocol.md](docs/protocol.md)),客户端零差异,
- **高性能内核**:桌面捕获与输入注入收进 **C++ nativecore**(C ABI 导出),Python/C++ 服务端复用;
- **零安装客户端**:浏览器原生 WebRTC,Web 页面即开即用。

## 能力矩阵

| 模块 | 语言 | WebRTC 底层 | 捕获/注入 | 目录 |
|---|---|---|---|---|
| 原生核心库 nativecore | C++ | - | DXGI 捕获 + SendInput 注入,C ABI | [server/cpp/nativecore](server/cpp/nativecore) |
| 服务端(Python 版,8080) | Python | aiortc | ctypes 接 nativecore;内置 mss fallback | [server/python](server/python) |
| 服务端(C++ 版,8081) | Python 壳 + C++ 核心 | libwebrtc(cloudrender_session.dll) | nativecore 直连(经 C ABI) | [server/cpp/shell](server/cpp/shell) |
| 服务端 SDK(C++) | C++ | libwebrtc | 直接链接 nativecore | [server/cpp/sdk](server/cpp/sdk) |
| 客户端 SDK | JavaScript | 浏览器原生 WebRTC | 键鼠/滚轮/触摸/手柄采集 | [client/javascript](client/javascript) |

## 架构

```
┌──────────────────────────────────────────────────────┐
│  C++ 原生核心库 nativecore(C ABI 导出)                  │
│  · 桌面/窗口捕获(DXGI Desktop Duplication,零拷贝纹理)   │
│  · OS 输入注入(SendInput 键鼠滚轮)                      │
└──────┬────────────────┬───────────────┘
  直接链接              ctypes
┌──────▼─────┐  ┌───────▼───────────┐
│ C++ SDK    │  │ Python SDK        │
│ libwebrtc  │  │ aiortc             │
└────────────┘  │ 内置 mss fallback │
                └────────────────────┘
            ▲ 统一协议(信令/输入/事件,语言无关)▲
┌──────────────────────────────────────────────────────┐
│ 客户端:浏览器 JavaScript SDK + Web UI(服务端内置托管)   │
└──────────────────────────────────────────────────────┘
```

仓库内有**两个可运行服务端**,共享同一协议(docs/protocol.md)与同一份 Web UI,可同时运行(端口不同):

| 端口 | 入口 | 媒体核心 | 启动 |
|---|---|---|---|
| 8080(Python 版) | `server/python`(cloudrender 包,含 Python SDK) | aiortc;捕获/注入优先 ctypes 接 nativecore,可回退 mss | `python -m cloudrender.server` |
| 8081(C++ 版) | `server/cpp/shell`(Python 信令壳 + C++ 媒体核心,自包含) | `cloudrender_session.dll`(libwebrtc + nativecore 直连,C ABI) | `python cpp_server.py --port 8081`(见 [server/cpp/shell/README.md](server/cpp/shell/README.md)) |

## 快速开始(本机云桌面 Demo)

无需 GPU/摄像头/编译 C++——Python 服务端自带纯 Python 捕获 fallback,一条命令跑通"浏览器操作自己的电脑"。

```bash
# 1. 启动 Python 云桌面服务器(需 Python 3.9+,Windows)
cd server/python
pip install -r requirements.txt
python -m cloudrender.server             # 默认绑定 0.0.0.0:8080,捕获整个桌面(--monitor 0;1=主显示器)

# 2. 打开 Web 客户端:服务端已内置 Web UI(页面与信令同端口,免额外静态服务)
# 浏览器直接打开 http://localhost:8080/,点击 Connect,再点击视频画面即可用键鼠控制本机
# 局域网另一台电脑:浏览器打开 http://<本机IP>:8080/(注意防火墙放行 8080)
# 也可以 file:// 直开 client/web/index.html(?ws= 参数可指向其他服务端地址)
```

> 说明:本机测试时目标窗口(浏览器)会递归显示在捕获画面中,属正常现象;体验演示请用两台机器(改 IP),或在页面中输入操作时终端保持聚焦。

**可选:8081(C++ 版服务端)** —— 与 8080 同协议、同页面,仅媒体核心不同(`cloudrender_session.dll`):先按
[server/cpp/sdk](server/cpp/sdk) 构建 DLL,再到 `server/cpp/shell` 运行 `python cpp_server.py --port 8081`;
管理员一键脚本 `tools\_restart_cpp_server_admin.cmd`(锁屏画面 worker 需要管理员权限)。
详见 [server/cpp/shell/README.md](server/cpp/shell/README.md)。

## 各语言 SDK 用法

### Python(完整示例见 server/python/cloudrender/server.py)

```python
from cloudrender import CloudRenderServer, MssScreenSource, WindowsInputInjector

server = CloudRenderServer(port=8080)
server.run(
    source_factory=lambda info: MssScreenSource(monitor=1, max_fps=30),
    injector_factory=lambda info: WindowsInputInjector(),
)
```

### C++(libwebrtc 会话 + nativecore)

```cpp
#include "cloudrender/session/cr_webrtc_session.hpp"
#include "cloudrender/session/nativecore_source.hpp"

cr::source::NativeCoreFrameSource source(/*monitor=*/0, /*max_fps=*/30);
cr::source::NativeCoreInjector injector;

cr::session::PeerSessionConfig config;                        // fps / ice_servers / prefer_h264
cr::session::PeerSession session(&source, &injector, config, {
    // on_signal:把 offer/ice/stats JSON 经 WebSocket 透传给客户端
});

std::string sdp;
session.CreateOffer(&sdp);            // 触发 on_signal("offer")
session.HandleSignal("answer", sdp);  // 客户端 answer/ice 原样传入
session.StartStreaming();             // 帧泵:拉最新帧 → BGRA→I420 → 编码推送
```

## 目录结构

```
cloudrender/
├── docs/
│   ├── protocol.md   # 信令/输入/事件协议(唯一契约)
│   └── integration.md # 部署、安全(UAC)、Unity/UE 引擎内嵌附赠说明
├── server/
│   ├── cpp/
│   │   ├── nativecore/   # C++ 原生核心:捕获 + 注入 + C ABI
│   │   ├── sdk/          # C++ SDK:协议层/会话层/cloudrender_session.dll(C ABI)
│   │   ├── shell/        # C++ 版服务端(8081):cpp_server.py 信令壳 + 依赖模块副本(自包含)
│   │   └── third_party/  # libwebrtc 预编译 facade 包(构建 sdk 会话层用)
│   └── python/           # Python 版服务端(8080)+ Python SDK(aiortc 包),入口:python -m cloudrender.server
├── client/
│   ├── javascript/       # JS 客户端 SDK(含契约测试 contract_test.mjs)
│   └── web/              # Web UI 页面(服务端内置托管,页面与 ws 同端口)
└── tools/                # 仓库工具:golden_wire.py(golden 双端生成)、诊断与提权启动脚本
```

## 安全提示

- 输入注入等同本机键鼠,请勿对不受信任的客户端开放;
- 默认监听本机/局域网;暴露公网必须自行前置 HTTPS/WSS 与认证(token);
- Windows 下注入受 UIPI 限制(无法向更高完整性级别的窗口注入),详见 [docs/integration.md](docs/integration.md)。