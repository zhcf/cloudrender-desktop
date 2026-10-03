# CloudRender 协议规范 v1

本协议是 CloudRender 云桌面各语言实现之间互通的唯一契约。JSON 字段一律 `snake_case`;二进制协议一律小端(Little Endian)。

适用两种渲染模式,两者使用同一条数据链路与消息结构:

1. **通用云桌面(VDI)模式**(主线):服务端渲染源 = OS 桌面/窗口截屏捕获,输入落点 = OS 级注入——任何软件零改造即可被流送与操作;
2. **引擎内嵌模式**(附赠):渲染引擎(Unity/UE/自研)主动把帧供给 SDK。

两种模式仅替换服务端 `FrameSource` / `InputInjector` 的实现,线上协议完全一致,客户端零差异。

```
URN 标识: urn:cloudrender:protocol:v1
```

---

## 1. 信令协议(WebSocket + JSON)

信令通道地址示例:`ws://host:8080/ws?token=xxx&width=1920&height=1080`

服务端可强制要求认证,`token` 为可选的鉴权凭据。信令仅用于建立会话、交换 SDP/ICE,会话建立后媒体与输入均走 WebRTC。

### 1.1 连接流程

```
客户端                                        服务端
   │── connect(client_info) ──────────────────►│
   │◄── connected(session_id, server_caps) ────│
   │◄── ready(resolution, codec, source) ──────│   ← 视频轨已就绪(SDP 协商前)
   │◄── offer(sdp) ────────────────────────────│   ← 必须晚于 connected:客户端靠 connected 创建 RTCPeerConnection
   │── answer(sdp) ────────────────────────────►│
   │── ice(candidate) ────────────────────────►│
   │◄── ice(candidate) ────────────────────────│
   │                        (DTLS/ICE 握手、DataChannel/media 建立)
   │── stats(...)  ◄──────────── 心跳 ─────────►│── stats(...)
   │── 键鼠/手柄/触摸 ...        (DataChannel "input",见 §3)
```

### 1.2 消息结构

每条消息一个 JSON 对象,必须携带 `type`:

```json
{ "type": "connect", "seq": 1, "payload": { ... } }
```

`seq` 为客户端递增序号,服务端回复带相同 `seq` 的消息用于关联(可选)。

### 1.3 客户端 → 服务端

| type | payload 字段 | 说明 |
|---|---|---|
| `connect` | `client_info{type: "web"\|"unity"\|"ue"\|"other", width, height, video_codecs[], audio: bool, platform, sdk_version}` | 建立会话。`width/height` 为期望分辨率;云桌面模式下服务端以捕获源实际分辨率为准(可缩放) |
| `answer` | `sdp: string` | WebRTC Answer SDP 文本或 `{type, sdp}` 对象 |
| `ice` | `candidate: string 或 {candidate, sdpMid, sdpMLineIndex}` | 客户端 ICE 候选 |
| `stats` | `stats{...}` | 客户端统计(见 §4),周期 1~2s |
| `lock_screen` | — | 锁定远端桌面(服务端执行 LockWorkStation,等效 Win+L)。锁屏后画面切到安全桌面,可直接输入密码解锁 |
| `disconnect` | `reason: string` | 主动断开 |

### 1.4 服务端 → 客户端

| type | payload 字段 | 说明 |
|---|---|---|
| `connected` | `session_id: string, server_caps{max_resolution{width,height}, codecs[], audio: bool, max_sessions}` | 会话已创建 |
| `offer` | `sdp: string` | WebRTC Offer SDP;服务端为 offer 方(云桌面标准做法,便于动态分辨率协商) |
| `ice` | `candidate` | 服务端 ICE 候选 |
| `ready` | `resolution{width,height}, codec, bitrate_kbps, source: "desktop"\|"window"\|"engine"` | 视频轨就绪 |
| `stats` | `stats{...}` | 服务端统计(§4) |
| `error` | `code: int, message: string, fatal: bool` | 错误。fatal 时服务端随后主动关闭 |
| `close` | `reason: string` | 会话结束 |

### 1.5 错误码

| code | 含义 |
|---|---|
| 4001 | 未认证(token 无效) |
| 4002 | 会话数已达上限 |
| 4003 | 参数非法(不支持的分辨率/编解码等) |
| 4100 | WebRTC 协商失败(无法建立 PeerConnection) |
| 4101 | ICE 超时 |
| 4102 | 编码器启动失败 |
| 5000 | 服务端内部错误 |

---

## 2. WebRTC 媒体与数据通道

### 2.1 媒体拓扑

- **方向**:单向 —— 服务端只发不收(recvonly → sendonly)。
- **视频轨**(mid 语义固定):编码优先级 `H264 > VP8 > AV1`,必须在 offer 的 m-line 中列出(云桌面场景 H264 便于 GPU 硬编/硬解)。默认 30fps。
- **音频轨**:可选,编码 `opus/48000/2`。默认关闭。
- **SDP 约定**:客户端 answer 时只能挑选 offer 中已列出的编解码与分辨率,不得新增。

### 2.2 DataChannel 清单

| 通道名 | 方向 | 可靠 | 用途 |
|---|---|---|---|
| `input` | C→S | 有序可靠 | 输入事件流(§3) |
| `events` | S→C | 有序可靠 | 系统事件:光标位、质量调节、文本、提示 |

两个通道由**服务端**创建(id 顺序不敏感,按 label 匹配)。

### 2.3 media 常见异常处理

- 客户端解码失败 → 通过 WebSocket 发 `{"type":"video_lost"}` 请求关键帧。
- 服务端收到 `video_lost` → 编码器发送下一个视频关键帧(IDR)。

---

## 3. 输入协议(DataChannel `input`,二进制)

### 3.0 帧格式

```
+-----------+-----------+-------------+-------------+------------------------+
|  magic(4) | ver(1)    | seq(4,u32)  | n(1,u8)     | events(n × 变长)        |
| "CRIN"    | 0x01      | 帧序号      | 事件个数    |                         |
+-----------+-----------+-------------+-------------+------------------------+
```

magic = `0x43 0x52 0x49 0x4E`("CRIN"),小端校验。

每个 event 结构:`| kind(1,u8) | ts(8,u64,毫秒) | body(变长) |`

### 3.1 键盘 `kind = 0x01`

```
| down(1,u8: 1按下/0抬起) | key_code(4,u32) | modifiers(1,u8 位掩码) | repeat(1,u8) |
```

- `key_code`:**FNV-1a(32 bit)哈希** 作用于 W3C `KeyboardEvent.code` 字符串(如 `"KeyA"`).各语言 SDK 实现同一个哈希函数即可互通;服务端协议层负责 `code → 平台键码(VK 等)` 的映射。
  - FNV-1a/32 参数:offset basis `0x811C9DC5`,prime `0x01000193`,小端输出。
- `modifiers` 位:0x01 Shift,0x02 Ctrl,0x04 Alt,0x08 Meta。

### 3.2 鼠标移动 `kind = 0x02`

```
| x(4,f32 归一化 0..1,相对画面左上角) | y(4,f32) | dx(2,i16,像素增量) | dy(2,i16) |
```

`dx/dy` 用于鼠标锁定(pointer-lock)模式(相对移动);非锁定模式客户端填 0,服务端用 `x/y` 按捕获分辨率换算绝对坐标注入。

### 3.3 鼠标按键 `kind = 0x03`

```
| button(1,u8: 0左 1中 2右 3侧键1 4侧键2) | down(1,u8) | clicks(1,u8) |
```

### 3.4 滚轮 `kind = 0x04`

```
| delta_x(4,f32) | delta_y(4,f32) | ctrl(1,u8: 用于缩放手势) |
```

### 3.5 手柄 `kind = 0x05`

```
| index(1,u8: 手柄序号 0..) | dpad(1,u8 位:上下左右) |
| buttons(4,u32 位掩码: 0=A 1=B 2=X 3=Y 4=LB 5=RB 6=Back 7=Start 8=LS按下 9=RS按下) |
| lx(4,f32 -1..1) | ly(4,f32) | rx(4,f32) | ry(4,f32) | lt(4,f32) | rt(4,f32) |
```

### 3.6 触摸 `kind = 0x06`

```
| id(1,u8 触点id) | phase(1,u8: 0开始 1移动 2结束 3取消) | x(4,f32 归一化) | y(4,f32) | pressure(4,f32 0..1) |
```

服务端按 noVNC 惯例把触摸映射为鼠标注入(Windows 无通用触摸注入 API):

- 单指:tap = 左键点击,拖动 = 左键拖拽;
- 双指:上下滑动 = 滚轮滚动(进入双指时先松开左键);
- 三指:tap = 右键。

### 3.7 文本 `kind = 0x07`

```
| length(2,u16) | utf8(length 字节) |
```

用于 IME 输入等无法用 key_code 表达的字符串(云桌面模式下服务端转换为 Unicode 按键注入)。

### 3.8 自定义/扩展 `kind = 0x7F`

```
| plugin_id(1,u8) | length(2,u16) | data(length 字节) |
```

---

## 4. 事件与统计

### 4.1 `events` 通道(S→C,二进制 = §3.0 结构用 magic `"CREV"`)

| kind | 名称 | 内容 |
|---|---|---|
| 0x01 | `cursor_pos` | `| x(f32) | y(f32) |` 服务端渲染出的光标位置(捕获含系统光标时可不发,客户端自行决定是否隐藏本地光标) |
| 0x02 | `quality` | `| target_bitrate(4,u32) | fps(1,u8) |` 码率自适应调整建议 |
| 0x03 | `toast` | `| length(2,u16) | utf8 |` 提示文本 |
| 0x04 | `pong` | `| seq(4,u32) |` 心跳应答 |

### 4.2 `stats` 消息(信令通道双向)

```json
{
  "type": "stats",
  "seq": 1,
  "payload": {
    "clock_ms": 1720000000000,
    "video": {"fps_sent": 30, "bitrate_kbps": 3500, "qp": 26, "encoder": "h264", "frame_drop": 2},
    "network": {"rtt_ms": 15, "packet_loss": 0.001, "jitter_ms": 2},
    "decode": {"fps": 29.8, "latency_ms": 42}        // 仅客户端填
  }
}
```

服务端据 `decode` 数据做码率自适应(区间 [min_bitrate, max_bitrate])。

---

## 5. 会话生命周期与断线重连

1. 客户端异常断开 → 服务端在 5s 内(ICE disconnected 判定,可配)结束会话,释放捕获/渲染实例。
2. 客户端重连:重新走完整 §1.1 流程,获新 `session_id`。
3. 服务端关闭 → 先发 `close`,再关闭 WebSocket。