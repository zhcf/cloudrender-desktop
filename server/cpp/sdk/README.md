# CloudRender C++ SDK

C++ 服务端 SDK:与 **nativecore 直接链接**(DXGI 捕获/输入注入零拷贝),通过 **libwebrtc** 完成编码推流与数据通道,是两种服务端实现中性能最高的全功能版本。

```
server/cpp/sdk/
├── include/cloudrender/
│   ├── wire/cr_wire.hpp            # 协议层:输入/事件帧编解码、FNV 键码表(纯标准库)
│   ├── capi/cr_session_api.h       # 会话核心 C ABI(cloudrender_session.dll 导出面)
│   └── session/
│       ├── cr_source.hpp           # FrameSource / InputInjector 抽象
│       ├── nativecore_source.hpp   # nativecore 适配器(帧槽 + 注入串行化)
│       └── cr_webrtc_session.hpp   # PeerSession(libwebrtc 会话层)
├── src/
│   ├── wire/cr_wire.cpp
│   ├── capi/cr_session_capi.cpp    # cloudrender_session.dll 导出实现(含信令排队)
│   └── session/
│       ├── cr_webrtc_session.cpp   # Offer/Answer/ICE、BGRA→I420、帧泵
│       └── nativecore_source.cpp
├── tests/                          # 契约测试 + 会话冒烟(golden.txt 由 tools/golden_wire.py 生成)
└── CMakeLists.txt
```

## 与 Python SDK 的关系

| 模块 | Python | C++(本目录) |
|---|---|---|
| 协议层 | cloudrender/protocol.py | **cr_wire**(字节级一致,契约测试保证) |
| 捕获/注入 | ctypes 接 nativecore | **直接链接 nativecore**(零拷贝) |
| WebRTC | aiortc | **libwebrtc**(预编译 facade 包,见构建 §3) |
| C ABI 出口 | - | **cloudrender_session.dll**(8081 信令壳 ctypes 加载) |

## 构建

### 1) 协议层 + 适配器(无需 libwebrtc,构建门槛最低)

依赖:nativecore 头文件 [`cr_api.h`](../nativecore/include/cloudrender/capi/cr_api.h);找不到时仅构建 `cr_wire`。

```bat
cmake -S server\cpp\sdk -B server\cpp\sdk\build
cmake --build server\cpp\sdk\build --config Release
```

产物:
- `cr_wire`(静态库):协议层,任何平台可构建;
- `cr_session_core`(静态库):nativecore 适配器 + 会话层桩(未开 `CR_BUILD_WEBRTC` 时 `PeerSession` 为空实现,头文件保持可用)。

Windows 下若 `cloudrender_nativecore` 的导入库位于 `../nativecore/build/{Release,Debug}`,会自动链接;否则由使用方自行提供(CMake 打印提示)。

### 2) 契约测试

```bat
cmake -S server\cpp\sdk -B server\cpp\sdk\build
cmake --build server\cpp\sdk\build --config Release
ctest --test-dir server\cpp\sdk\build -C Release --output-on-failure
```

`cr_contract_test` 读取 `tests/golden.txt`(由 `python tools/golden_wire.py` 在仓库根运行生成,勿手改 hex),与 Python 参考实现做 **字节级比对**:8 种 kind 输入帧、事件通道帧、FNV-1a32 基准向量、解码往返、非法帧拒绝。期望输出 `CONTRACT_OK input=187B events=68B`。

会话核心冒烟(DLL 已构建时,在仓库根运行):`python server\cpp\sdk\tests\session_capi_smoke.py`——真实抓屏 + create_offer + 信令轮询,通过输出 `SESSION_CAPI_SMOKE_OK`;`webrtc_smoke.cpp` / `webrtc_probe.cpp` 为构建期的会话层探针。

### 3) 完整会话层 + cloudrender_session.dll(8081 用的媒体核心)

依赖预编译 libwebrtc facade 包(`server/cpp/third_party/libwebrtc/libwebrtc-x64-release/`,含 `include/libwebrtc.h` 与 `lib/libwebrtc.dll`、`lib/libwebrtc.dll.lib`);CMake 默认自动探测该路径,亦可用 `-DCR_WEBRTC_ROOT=<dir>` 覆盖:

```bat
cmake -S server\cpp\sdk -B server\cpp\sdk\build -DCR_BUILD_WEBRTC=ON
cmake --build server\cpp\sdk\build --config Release
```

产物(`build/Release/`):

- `cloudrender_session.dll`(`cr_session_dll` 目标):导出 C ABI 见 `include/cloudrender/capi/cr_session_api.h`,供 Python ctypes 对接(8081 信令壳);
- `libwebrtc.dll` / `nativecore.dll`:POST_BUILD 自动复制到产物目录(导入表按真实 DLL 名记录,必须随 `cloudrender_session.dll` 同行);
- `cr_session_core.lib` / `cr_wire.lib` 静态库。

编译宏 `CR_HAVE_WEBRTC LIB_WEBRTC_API_DLL RTC_DESKTOP_DEVICE` 必须与预编译 DLL 一致(否则工厂虚表槽位错位),CMake 已内置;另链接系统库 `winmm ws2_32 secur32 crypt32 iphlpapi d3d11 dxgi`。

## 最小用法(伪代码)

```cpp
#include "cloudrender/session/cr_webrtc_session.hpp"
#include "cloudrender/session/nativecore_source.hpp"

cr::source::NativeCoreFrameSource source(/*monitor=*/-1, /*max_fps=*/30);  // -1 = 主显示器
cr::source::NativeCoreInjector injector;

cr::session::PeerSessionConfig config;
config.fps = 30;
config.ice_servers = {"stun:stun.l.google.com:19302"};

cr::session::PeerSession session(
    &source, &injector, config,
    {/* on_signal:把 offer/ice JSON 经 WebSocket 发给客户端;on_closed:记录并清理 */});

std::string sdp;
session.CreateOffer(&sdp);          // 触发 on_signal("offer", ...)
session.HandleSignal("answer", sdp); // 客户端 answer/ice JSON 原样传入
session.StartStreaming();            // 帧泵:拉最新帧 → BGRA→I420 → 推送编码
```

语义与 Python `session.py` 一致:`FrameSource::TryTakeLatest` 丢旧帧只送最新;`MOVE` 先相对后绝对(`x*source.width()`);`WHEEL` dy×120;手柄/触摸/自定义事件 v1 注入器不处理(协议层已解出,见 `ApplyInput`)。

## 构建与验证状态

- 已在本机(MSVC/VS2022 + 预编译 libwebrtc facade 包)实际构建通过,`build/Release/` 内含 `cloudrender_session.dll`、`libwebrtc.dll`、`nativecore.dll`;
- 契约测试 `ctest` 通过(`CONTRACT_OK input=187B events=68B`);会话冒烟 `SESSION_CAPI_SMOKE_OK`;
- [server/cpp/shell](../shell/README.md)(8081 信令壳)已加载该 DLL 端到端运行。

## 已知限制

- 编码器由 libwebrtc 内置工厂提供;H264/VP8 顺序按工厂 SDP 为准,v1 不重排(截屏场景 H264 优先,可用外部编码器工厂注入)。
- 捕获上限与权限(桌面重复、UIPI)依赖 nativecore,见根 README 与 docs/integration.md。