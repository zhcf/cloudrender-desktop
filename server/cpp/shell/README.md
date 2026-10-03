# CloudRender C++ 服务端信令壳(8081)

`cloudrender_session.dll`(C++ 会话核心,见 [server/cpp/sdk](../sdk/README.md))的 Python 信令壳:
信令协议与 Web UI 同 8080(aiortc 版)完全一致,媒体会话/抓屏/注入全部在 DLL 内完成,
本进程只做信令桥接与会话治理。**完全自包含**:不依赖 `server/python` 的 cloudrender 包。

## 目录内容

```
server/cpp/shell/
├── cpp_server.py        # 8081 入口:aiohttp 信令壳(CLI 见下)
├── cpp_session.py       # CppPeerSession:cloudrender_session.dll 会话的异步封装
├── capi.py              # DLL 的 ctypes 绑定(加载探测 + cr_session_poll_signal 轮询)
├── secure_ops.py        # 锁屏/安全桌面辅助(锁定工作站、安全桌面检测、UIPI 提示)
├── protocol.py          # 协议层(与 cloudrender 包内原件同步副本)
├── webui.py             # 内置 Web UI 托管(同上)
├── wdesktop.py          # 安全桌面 worker 客户端(同上)
├── wdesktop_worker.py   # 安全桌面 worker 本体(同上)
├── winlogon.py          # worker 提权 spawn(同上)
└── _wdesktop_worker_cpp.log   # 锁屏 worker 运行日志(运行时生成)
```

> 与 `server/python/cloudrender/` 同名的模块(protocol/webui/wdesktop/wdesktop_worker/winlogon)
> 为镜像副本,修改任一共享逻辑须两处同步;secure_ops.py 为本壳专属独立实现。

## 前置:构建 DLL

媒体核心 `cloudrender_session.dll` 必须先构建(见 [sdk README](../sdk/README.md)):

```bat
cmake -S server\cpp\sdk -B server\cpp\sdk\build -DCR_BUILD_WEBRTC=ON
cmake --build server\cpp\sdk\build --config Release
```

DLL 加载探测顺序(见 capi.py):

1. 环境变量 `CLOUDRENDER_SESSION_DLL`(文件路径或目录);
2. `server/cpp/sdk/build/{Release,Debug}/cloudrender_session.dll`。

`libwebrtc.dll` 与 `nativecore.dll` 由 sdk 构建 POST_BUILD 自动部署到 DLL 同目录。

## 启动

```bash
cd server/cpp/shell
python cpp_server.py --port 8081
# 浏览器打开 http://localhost:8081/(内置 Web UI,与 8080 版同页面)
```

管理员一键脚本 `tools\_restart_cpp_server_admin.cmd`:自提权 → 停旧 8081 实例 →
暂存 DLL(build_sw→build)→ 以 `--port 8081 --fps 60 --bitrate 10000` 启动,
控制台输出重定向到 `tools/_cpp_server_console.log`。

> 管理员权限用于锁屏画面 worker(winlogon 令牌 spawn 需 SeDebugPrivilege);
> 普通权限也能启动,但锁屏期间降级为静帧。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | 0.0.0.0 | 监听地址 |
| `--port` | 8080 | 信令端口(实际使用 8081,与 aiortc 版区分) |
| `--token` | 无 | 连接鉴权 token(缺省不校验) |
| `--max-sessions` | 1 | 并发会话上限(nativecore 抓屏同显示器互斥) |
| `--fps` | 30 | 帧率上限 1..60 |
| `--bitrate` | 10000 | 视频目标码率 kbps(固定值;0=自适应) |
| `--monitor` | -1 | 捕获显示器索引(-1=主显示器) |
| `--window` | 0 | 捕获窗口句柄(如 0x1A2B3C);非 0 时按窗口捕获 |
| `--ice` | 无 | STUN 服务器(可多次,如 --ice stun:host:3478);缺省=局域网直连 |
| `--dll` | 自动 | cloudrender_session.dll 路径(缺省自动探测构建产物) |
| `--wdesktop-port` | 45995 | 安全桌面 worker 端口(与 aiortc 版 45990 隔离) |

## 与 8080(aiortc 版)的关系

| 维度 | 8080 Python 版 | 8081 本壳 |
|---|---|---|
| 入口 | `server/python`:`python -m cloudrender.server` | `server/cpp/shell`:`python cpp_server.py --port 8081` |
| 媒体核心 | aiortc(Python 实现) | `cloudrender_session.dll`(libwebrtc C++,信令轮询回传) |
| 捕获/注入 | ctypes 接 nativecore,可回退 mss | nativecore 直连(经 DLL) |
| 信令/页面 | 同一协议(docs/protocol.md)与同一份 Web UI | 相同 |
| 锁屏 worker 端口 | 45990 | 45995 |

两版可同时运行(端口与 worker 端口均隔离),浏览器接口一致:connect → connected → ready → offer/answer/ice 循环。