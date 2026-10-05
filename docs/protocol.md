# CloudRender protocol specification v1

This protocol is the single contract for interoperability between all CloudRender language implementations. JSON fields are always `snake_case`; binary protocols are always little-endian.

It applies to two rendering modes that share one data path and message structure:

1. **Generic cloud desktop (VDI) mode** (mainline): the server render source = OS desktop/window screen capture, and input lands as OS-level injection — any software can be streamed and operated with zero modification;
2. **Engine embedding mode** (bonus): a rendering engine (Unity/UE/custom) supplies frames to the SDK actively.

Both modes only swap the server `FrameSource` / `InputInjector` implementations; the wire protocol is fully identical and clients see zero difference.

```
URN: urn:cloudrender:protocol:v1
```

---

## 1. Signaling protocol (WebSocket + JSON)

Example signaling channel address: `ws://host:8080/ws?token=xxx&width=1920&height=1080`

The server may require authentication; `token` is the optional credential. Signaling is only used to establish the session and exchange SDP/ICE; once the session is up, both media and input run over WebRTC.

### 1.1 Connection flow

```
Client                                        Server
   │── connect(client_info) ──────────────────►│
   │◄── connected(session_id, server_caps) ────│
   │◄── ready(resolution, codec, source) ──────│   ← video track ready (before SDP negotiation)
   │◄── offer(sdp) ────────────────────────────│   ← must come after connected: the client creates its RTCPeerConnection on connected
   │── answer(sdp) ────────────────────────────►│
   │── ice(candidate) ────────────────────────►│
   │◄── ice(candidate) ────────────────────────│
   │                        (DTLS/ICE handshake, DataChannel/media established)
   │── stats(...)  ◄──────────── heartbeat ────►│── stats(...)
   │── keyboard/mouse/gamepad/touch ...        (DataChannel "input", see §3)
```

### 1.2 Message structure

Each message is one JSON object and must carry `type`:

```json
{ "type": "connect", "seq": 1, "payload": { ... } }
```

`seq` is a client-incrementing sequence number; server replies carrying the same `seq` are used for correlation (optional).

### 1.3 Client → Server

| type | payload fields | description |
|---|---|---|
| `connect` | `client_info{type: "web"\|"unity"\|"ue"\|"other", width, height, video_codecs[], audio: bool, platform, sdk_version}` | Establish the session. `width/height` are the desired resolution; in cloud desktop mode the server uses the capture source's actual resolution (scalable) |
| `answer` | `sdp: string` | WebRTC Answer SDP text, or a `{type, sdp}` object |
| `ice` | `candidate: string or {candidate, sdpMid, sdpMLineIndex}` | Client ICE candidate |
| `stats` | `stats{...}` | Client stats (see §4), every 1–2s |
| `lock_screen` | — | Lock the remote desktop (the server calls LockWorkStation, equivalent to Win+L). After locking, the picture switches to the secure desktop and the password can be typed directly to unlock |
| `disconnect` | `reason: string` | Disconnect voluntarily |

### 1.4 Server → Client

| type | payload fields | description |
|---|---|---|
| `connected` | `session_id: string, server_caps{max_resolution{width,height}, codecs[], audio: bool, max_sessions}` | Session created |
| `offer` | `sdp: string` | WebRTC Offer SDP; the server is the offerer (standard for cloud desktops; enables dynamic resolution negotiation) |
| `ice` | `candidate` | Server ICE candidate |
| `ready` | `resolution{width,height}, codec, bitrate_kbps, source: "desktop"\|"window"\|"engine"` | Video track ready |
| `stats` | `stats{...}` | Server stats (§4) |
| `error` | `code: int, message: string, fatal: bool` | Error. When fatal, the server closes the connection afterwards |
| `close` | `reason: string` | Session ended |

### 1.5 Error codes

| code | meaning |
|---|---|
| 4001 | unauthenticated (invalid token) |
| 4002 | session limit reached |
| 4003 | invalid parameters (unsupported resolution/codec etc.) |
| 4100 | WebRTC negotiation failed (cannot establish PeerConnection) |
| 4101 | ICE timeout |
| 4102 | encoder startup failed |
| 5000 | server internal error |

---

## 2. WebRTC media and data channels

### 2.1 Media topology

- **Direction**: unidirectional — the server only sends (recvonly → sendonly).
- **Video track** (fixed mid semantics): codec priority `H264 > VP8 > AV1`, which must be listed in the offer m-lines (H264 enables GPU hardware encode/decode for cloud desktop scenarios). Default 30fps.
- **Audio track**: optional, codec `opus/48000/2`. Off by default.
- **SDP rule**: the client answer may only pick codecs and resolutions already listed in the offer; it must not add new ones.

### 2.2 DataChannel list

| channel | direction | reliable | purpose |
|---|---|---|---|
| `input` | C→S | ordered reliable | input event stream (§3) |
| `events` | S→C | ordered reliable | system events: cursor position, quality adjustments, text, notices |

Both channels are created by the **server** (id order is irrelevant; matched by label).

### 2.3 Common media exception handling

- Client decode failure → send `{"type":"video_lost"}` over the WebSocket to request a keyframe.
- Server receives `video_lost` → the encoder emits the next video keyframe (IDR).

---

## 3. Input protocol (DataChannel `input`, binary)

### 3.0 Frame format

```
+-----------+-----------+-------------+-------------+------------------------+
|  magic(4) | ver(1)    | seq(4,u32)  | n(1,u8)     | events(n × variable)   |
| "CRIN"    | 0x01      | frame seq   | event count |                        |
+-----------+-----------+-------------+-------------+------------------------+
```

magic = `0x43 0x52 0x49 0x4E` ("CRIN"), little-endian check.

Each event is: `| kind(1,u8) | ts(8,u64, milliseconds) | body(variable) |`

### 3.1 Keyboard `kind = 0x01`

```
| down(1,u8: 1 press / 0 release) | key_code(4,u32) | modifiers(1,u8 bitmask) | repeat(1,u8) |
```

- `key_code`: **FNV-1a (32-bit) hash** of the W3C `KeyboardEvent.code` string (e.g. `"KeyA"`). Every language SDK implementing the same hash interoperates; the server protocol layer maps `code → platform key code (VK etc.)`.
  - FNV-1a/32 parameters: offset basis `0x811C9DC5`, prime `0x01000193`, little-endian output.
- `modifiers` bits: 0x01 Shift, 0x02 Ctrl, 0x04 Alt, 0x08 Meta.

### 3.2 Mouse move `kind = 0x02`

```
| x(4,f32 normalized 0..1, relative to the top-left of the picture) | y(4,f32) | dx(2,i16, pixel delta) | dy(2,i16) |
```

`dx/dy` are used by pointer-lock mode (relative movement); in non-lock mode the client sends 0 and the server converts `x/y` to absolute coordinates using the capture resolution.

### 3.3 Mouse buttons `kind = 0x03`

```
| button(1,u8: 0 left / 1 middle / 2 right / 3 side1 / 4 side2) | down(1,u8) | clicks(1,u8) |
```

### 3.4 Wheel `kind = 0x04`

```
| delta_x(4,f32) | delta_y(4,f32) | ctrl(1,u8: for pinch zoom) |
```

### 3.5 Gamepad `kind = 0x05`

```
| index(1,u8: gamepad index 0..) | dpad(1,u8 bits: up/down/left/right) |
| buttons(4,u32 bitmask: 0=A 1=B 2=X 3=Y 4=LB 5=RB 6=Back 7=Start 8=LS press 9=RS press) |
| lx(4,f32 -1..1) | ly(4,f32) | rx(4,f32) | ry(4,f32) | lt(4,f32) | rt(4,f32) |
```

### 3.6 Touch `kind = 0x06`

```
| id(1,u8 touch id) | phase(1,u8: 0 start / 1 move / 2 end / 3 cancel) | x(4,f32 normalized) | y(4,f32) | pressure(4,f32 0..1) |
```

The server maps touches to mouse injection following the noVNC convention (Windows has no generic touch-injection API):

- One finger: tap = left click, drag = left drag;
- Two fingers: vertical swipe = wheel scroll (release the left button when the second finger lands);
- Three fingers: tap = right click.

### 3.7 Text `kind = 0x07`

```
| length(2,u16) | utf8(length bytes) |
```

For strings that cannot be expressed with key_code, such as IME input (in cloud desktop mode the server converts them to Unicode key injection).

### 3.8 Custom/extension `kind = 0x7F`

```
| plugin_id(1,u8) | length(2,u16) | data(length bytes) |
```

---

## 4. Events and stats

### 4.1 `events` channel (S→C, binary = §3.0 layout with magic `"CREV"`)

| kind | name | content |
|---|---|---|
| 0x01 | `cursor_pos` | `| x(f32) | y(f32) |` cursor position rendered by the server (can be omitted when the capture includes the system cursor; the client decides whether to hide the local cursor) |
| 0x02 | `quality` | `| target_bitrate(4,u32) | fps(1,u8) |` bitrate adaptation hint |
| 0x03 | `toast` | `| length(2,u16) | utf8 |` notice text |
| 0x04 | `pong` | `| seq(4,u32) |` heartbeat reply |

### 4.2 `stats` message (both directions on the signaling channel)

```json
{
  "type": "stats",
  "seq": 1,
  "payload": {
    "clock_ms": 1720000000000,
    "video": {"fps_sent": 30, "bitrate_kbps": 3500, "qp": 26, "encoder": "h264", "frame_drop": 2},
    "network": {"rtt_ms": 15, "packet_loss": 0.001, "jitter_ms": 2},
    "decode": {"fps": 29.8, "latency_ms": 42}        // client fills only
  }
}
```

The server uses the `decode` data for bitrate adaptation (range [min_bitrate, max_bitrate]).

---

## 5. Session lifecycle and reconnection

1. Client drops unexpectedly → the server ends the session within 5s (ICE disconnected detection, configurable) and releases the capture/render instances.
2. Client reconnect: repeat the full §1.1 flow to obtain a new `session_id`.
3. Server shutdown → send `close` first, then close the WebSocket.