# CloudRender JavaScript 客户端 SDK

浏览器原生 WebRTC 的通用云桌面客户端:视频播放 + 键鼠/滚轮/触摸/手柄采集回传,零安装。Web 演示页面见仓库 `client/web/`(各服务端内置托管,亦可 file:// 直开)。

```
client/javascript/
├── src/cloudrender.js        # 客户端 SDK(ES module,零依赖)
├── tools/contract_test.mjs   # 协议契约测试(JS ↔ Python golden 字节比对)
│   └── golden.json           # 由 python tools/golden_wire.py(仓库根运行)生成
└── package.json
```

## 快速开始

1. 先启动任一服务端(8080 Python 版或 8081 C++ 版,见根 README「两个可运行服务端」);
2. 浏览器打开服务端地址(如 `http://localhost:8080/`;8081 版为 `http://localhost:8081/`),点击 Connect,再点击视频画面即进入操作模式;

## SDK 用法

```js
import { CloudRenderClient } from "./src/cloudrender.js";

const client = new CloudRenderClient("ws://192.168.1.10:8080/ws?token=secret", {
  video: document.querySelector("video"),   // 可选:自动绑定流
});
client.on("connected", (payload) => console.log("已连接", payload));  // 含 server_caps
client.on("ready", ({ resolution, codec }) => console.log("视频就绪", resolution, codec));
client.on("track", (track, stream) => { /* stream 可直接给 video.srcObject */ });
client.on("stats", (stats) => { /* fps/bitrate/rtt 面板 */ });
client.on("close", (reason) => console.warn("断开", reason));

await client.connect();   // 自动完成 connect→answer→ice 信令与 DataChannel 建立

// 输入回传(内部 16ms 批量 flush 成二进制输入帧)
client.keyDown("KeyA");
client.keyUp("KeyA");
client.mouseMove(0.5, 0.25, -3, 2);   // 归一化坐标 + 相对双值(指针锁定用)
client.mouseButton(0, true, 1);       // button:0 左 / 1 中 / 2 右
client.wheel(0, 1.0);
client.touchEvent(2, 1, 0.3, 0.4, 0.9);
client.gamepadState(0, 0b11, 3, [0.1, -0.2, 0, -0.5, 0, 0.5]);
client.sendText("你好");
client.custom(9, new Uint8Array([0x78, 0x79, 0x7a]));
client.requestKeyframe();            // 画面劣化时请求关键帧(信令 video_lost)
```

### 事件协议通道

服务端经 DataChannel `"events"` 下发 `CREV` 二进制帧,SDK 以 `eventframe` 事件转发原始 `ArrayBuffer`,可用静态方法解析:

```js
client.on("eventframe", (buf) => {
  for (const ev of CloudRenderClient.parseEventFrame(buf)) {
    if (ev.kind === 0x01) console.log("光标", ev.x, ev.y);       // cursor_pos
    if (ev.kind === 0x02) console.log("质量", ev.bitrate, ev.fps); // quality
    if (ev.kind === 0x03) console.log("提示", ev.text);           // toast
  }
});
```

## 协议一致性验证

```bash
python tools/golden_wire.py   # 在仓库根运行:生成 golden.json(本目录)与 golden.txt(供 C++)
npm test                      # 字节级比对:input/events 帧、FNV 向量、坏帧拒绝
```

期望输出 `CONTRACT_OK input=187B events=68B`。SDK 与 Python/C++ 服务端的编解码完全一致(规范见 docs/protocol.md)。

## 浏览器要求

- 现代 Chrome/Edge/Firefox/Safari(原生 `RTCPeerConnection` + `DataChannel`);
- 公网部署页面必须 HTTPS(页面 HTTP 时 `ws://` 被混合内容策略拦截),见 docs/integration.md §1.3;
- 鼠标指针锁定(Pointer Lock)需要用户点击画面触发,相对移动与归一化绝对坐标双模式均由 `mouseMove` 覆盖。