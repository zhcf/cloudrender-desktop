# CloudRender 集成与部署指南

协议规范见 [protocol.md](protocol.md)。本文覆盖三类问题:**怎么部署**(拓扑与两种服务端)、**Windows 权限边界**(UAC/UIPI)、**附赠的引擎内嵌模式**(Unity/UE/自研引擎直接供帧)。

---

## 1. 部署拓扑

### 1.1 本机自测(开箱即跑)

见根 README 快速开始:Python 服务端 + 浏览器 Demo,零编译。注意自测时浏览器在捕获画面内会递归显示自己,属正常现象。

### 1.2 局域网(推荐起步)

- 服务端绑定 `0.0.0.0:8080`(Python `server` 默认),客户端连 `ws://<服务器IP>:8080/ws?token=xxx`;
- 建议配置 `token`(信令握手第一关,错误码 4001),媒体段本身由 DTLS 加密;
- 打开防火墙入站 TCP 8080,或改 `--port`。

### 1.3 公网(必须 HTTPS/WSS + 认证)

浏览器页面经 HTTPS 加载后,页面内的 `ws://` 会被当作混合内容拦截,因此:

```nginx
# 反代示例:Nginx 终结 TLS,转发到本地 8080
location /ws {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

- 必须启用 token 认证(反代也可再加一层鉴权/Basic/IP 白名单);
- **不要把输入注入服务裸暴露公网**:注入等同本机键鼠,客户端可操作服务端机器。生产建议置于 VPN/专网内。

## 2. 两种服务端部署

| 服务端 | 适用 | 关键命令 | 编译要求 |
|---|---|---|---|
| Python(aiortc) | 开箱即跑、黄金参照 | `python -m cloudrender.server --port 8080` | 仅 Python 3.9+ |
| C++(libwebrtc) | 高性能全功能 | cmake `-DCR_BUILD_WEBRTC=ON` 构建后接入信令层 | 预编译 libwebrtc M114+ |

要点:

- **Python**:默认优先 `NativeCoreCaptureSource`(检测到 `nativecore.dll`),否则自动回退 `MssScreenSource`;`--force-fallback` 强制纯 Python 路径。injector 默认 `WindowsInputInjector`(ctypes SendInput,零编译);
- **C++**:所有输入/捕获直连 nativecore;信令传输层(WebSocket)由上层接入,`PeerSession::Callbacks.on_signal` 把 offer/ice JSON 转发即可(见 server/cpp/sdk/README.md)。

## 3. Windows 权限边界(UAC / UIPI)

输入注入走 `SendInput`/`SetCursorPos`,受 **UIPI(User Interface Privilege Isolation)** 约束:

- 注入进程只能操作**完整性级别不高于自身**的窗口。普通权限的服务端可注入普通应用;以管理员运行服务端后,反而**无法注入**普通权限窗口(如计算器)**也无法被其测试**;
- **UAC 提示框(UAC Secure Desktop)期间**:安全桌面与用户桌面隔离,DXGI Desktop Duplication 捕获不到、`SendInput` 注入不进去,表现为"画面冻结、键鼠失灵"。远程运维场景请:
  - 目标机将 UAC 降为"不提示"(牺牲安全,不推荐生产);或
  - 服务端以与用户会话相同完整性级别运行,并在桌面侧用组策略调整 UAC 行为;
- **服务方式运行(Session 0)不可行**:Session 0 无交互桌面,DXGI 无法捕获用户桌面。请以**登录用户会话**内的计划任务/自启动方式运行服务端;
- 注入产生的合成输入**不区分来源**:防火墙/安全软件可能拦截;游戏类反作弊环境亦可能屏蔽 `SendInput`。

## 4. 引擎内嵌模式(附赠说明)

云桌面主线之外,SDK 可嵌入 Unity/UE/自研引擎:引擎**主动供帧**,输入回传由引擎自行解释。线上协议完全一致(信令 `client_info.type` 填 `"unity"`/`"ue"`),客户端零差异——只需替换服务端的 `FrameSource` / `InputInjector` 实现:

### Python

实现 `cloudrender.capture.FrameSource`(输出 BGRA32 `CapturedFrame`)+ `cloudrender.inject.InputInjector`,传给 `CloudRenderServer.run(source_factory=..., injector_factory=...)`。参考 `MssScreenSource`/`WindowsInputInjector`。

### C++

实现 `cr::source::FrameSource` / `cr::source::InputInjector`(纯虚,见 `include/cloudrender/session/cr_source.hpp`),构造 `cr::session::PeerSession` 即可。UE 下典型接法:引擎渲染线程把 backbuffer 拷贝为 BGRA 供帧、`PeerSession` 回调对接引擎内 WebSocket,Win64 直接链接 `cr_wire` + `cr_session_core`。

### 任意语言(经 C ABI)

`cr_capture_*` / `cr_inject_*` 导出(见 `server/cpp/nativecore/include/cloudrender/capi/cr_api.h`)为不透明句柄 + 回调 + 固定结构体 ABI,任何语言可 ctypes/FFI 复用捕获与注入;信令/媒体仍需一种 WebRTC 实现承载。

## 5. 性能调优

- **首选 nativecore**:DXGI Desktop Duplication 零拷贝;mss/GDI 路径为兜底(CPU 抓屏);
- **上限 1920x1080@30**(可配):分辨率/帧率与编码器开销成正比,局域网可调高;
- **H264 优先**:截屏场景码率高,浏览器硬解 H264 功耗低;
- 网络劣化→客户端发 `video_lost` 信令,服务端强制关键帧(协议 §2);
- 输入帧客户端侧 16ms 批量 flush,服务端只管按帧解析,无需回调节流。

## 6. 常见问题

| 症状 | 原因与处理 |
|---|---|
| 浏览器里画面无限递归 | 本机自测时捕获了显示页面的显示器,切到两台机器验证 |
| 视频黑屏/花屏后不恢复 | 丢关键帧:客户端点"请求关键帧"或重连;网络丢包高时调低分辨率 |
| 键鼠没反应 | 目标窗口完整性级别高于注入进程(UIPI,见 §3);或未点击视频画面取得焦点 |
| `nativecore.dll 未找到` | 先编译 `server/cpp/nativecore`(CMake),或使用 mss/GDI fallback |
| 双显示器坐标错位 | `MOVE` 归一化坐标以捕获源宽高为基准;注入用虚拟屏 SM_X/YVIRTUALSCREEN 换算,换个显示器顺序需重连 |
| 防火墙不通 | 放行 TCP 信令端口;WebRTC 媒体端口由 ICE 协商,企业网需放行 UDP 或配置 TURN |
| 中文输入乱码 | TEXT 事件为 UTF-8 字节,注入端已按 UTF-8 转 Unicode 键事件;IME 组合输入建议走系统输入法(v2 优化) |