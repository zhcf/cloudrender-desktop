# CloudRender Integration & Deployment Guide

Protocol spec: [protocol.md](protocol.md). This document covers three areas: **how to deploy** (topology and the two servers), **Windows privilege boundaries** (UAC/UIPI), and the **bonus engine-embedding mode** (Unity/UE/custom engines supplying frames directly).

---

## 1. Deployment topologies

### 1.1 Local self-test (runs out of the box)

See the root README quick start: Python server + browser demo, no compilation. Note that during self-test the browser shows itself recursively inside the captured image — expected behavior.

### 1.2 LAN (recommended starting point)

- The server binds `0.0.0.0:8080` (Python `server` default); clients connect to `ws://<server-IP>:8080/ws?token=xxx`;
- Configure `token` (the first gate of the signaling handshake; error code 4001 is returned on mismatch); the media path itself is encrypted by DTLS;
- Open inbound TCP 8080 in the firewall, or change `--port`.

### 1.3 Public internet (HTTPS/WSS + authentication required)

Once the page is loaded over HTTPS, `ws://` inside the page is blocked as mixed content, so:

```nginx
# reverse proxy example: Nginx terminates TLS and forwards to local 8080
location /ws {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

- Token authentication must be enabled (the reverse proxy can add another layer: auth/Basic/IP allowlist);
- **Never expose the injection service directly to the public internet**: injection equals local keyboard/mouse and a client can operate the server machine. For production, place it inside a VPN/private network.

## 2. Deploying the two servers

| Server | Use case | Key command | Build requirement |
|---|---|---|---|
| Python (aiortc) | Runs out of the box; golden reference | `python -m cloudrender.server --port 8080` | Python 3.9+ only |
| C++ (libwebrtc) | High performance, full features | build with cmake `-DCR_BUILD_WEBRTC=ON`, then attach the signaling layer | prebuilt libwebrtc M114+ |

Key points:

- **Python**: prefers `NativeCoreCaptureSource` by default (when `nativecore.dll` is detected), otherwise falls back to `MssScreenSource` automatically; `--force-fallback` forces the pure-Python path. The injector is `WindowsInputInjector` by default (ctypes SendInput, no compilation);
- **C++**: all input/capture links nativecore directly; the signaling transport (WebSocket) is attached by the host layer — just forward offer/ice JSON from `PeerSession::Callbacks.on_signal` (see server/cpp/sdk/README.md).

## 3. Windows privilege boundaries (UAC / UIPI)

Input injection uses `SendInput`/`SetCursorPos` and is constrained by **UIPI (User Interface Privilege Isolation)**:

- An injecting process can only operate windows whose **integrity level is not higher than its own**. A normal-privilege server can inject into normal apps; after running the server as administrator it conversely **cannot inject into** normal-privilege windows (e.g. Calculator), **nor can it be tested against them**;
- **During the UAC prompt (UAC Secure Desktop)**: the secure desktop is isolated from the user desktop — DXGI Desktop Duplication captures nothing and `SendInput` injects nothing, which shows up as "frozen picture, dead keyboard/mouse". For remote administration either:
  - Set UAC to "never notify" on the target machine (weakens security; not recommended for production); or
  - Run the server at the same integrity level as the user session and tune UAC behavior via Group Policy on the desktop side;
- **Running as a service (Session 0) does not work**: Session 0 has no interactive desktop, so DXGI cannot capture the user desktop. Run the server from a scheduled task/startup entry inside the **logged-on user session**;
- Synthetic input from injection is **not distinguishable by origin**: firewalls/security software may block it; game anti-cheat environments may also block `SendInput`.

## 4. Engine embedding mode (bonus notes)

Beyond the main cloud desktop path, the SDK can be embedded into Unity/UE/custom engines: the engine **supplies frames actively**, and input uplink is interpreted by the engine itself. The wire protocol is fully identical (set signaling `client_info.type` to `"unity"`/`"ue"`), and clients see zero difference — you only replace the server-side `FrameSource` / `InputInjector` implementation:

### Python

Implement `cloudrender.capture.FrameSource` (returning BGRA32 `CapturedFrame`) + `cloudrender.inject.InputInjector` and pass them to `CloudRenderServer.run(source_factory=..., injector_factory=...)`. See `MssScreenSource`/`WindowsInputInjector` as references.

### C++

Implement `cr::source::FrameSource` / `cr::source::InputInjector` (pure virtual; see `include/cloudrender/session/cr_source.hpp`) and construct `cr::session::PeerSession`. Typical UE wiring: the engine render thread copies the backbuffer to BGRA for the frame source, `PeerSession` callbacks connect to the engine's WebSocket, and Win64 links `cr_wire` + `cr_session_core` directly.

### Any language (via the C ABI)

The `cr_capture_*` / `cr_inject_*` exports (see `server/cpp/nativecore/include/cloudrender/capi/cr_api.h`) are an opaque handle + callback + fixed-struct ABI that any language can reuse through ctypes/FFI for capture and injection; signaling/media still need a WebRTC implementation to carry them.

## 5. Performance tuning

- **Prefer nativecore**: DXGI Desktop Duplication is zero-copy; the mss/GDI path is a fallback (CPU screen capture);
- **Cap of 1920x1080@30** (configurable): resolution/frame rate scale encoder cost; LAN deployments can go higher;
- **H264 preferred**: screen content has high bitrate and browser H264 hardware decoding is power-efficient;
- Network degradation → the client sends a `video_lost` signal and the server forces a keyframe (protocol §2);
- Input frames are batched client-side every 16ms; the server just parses frame by frame, no callback throttling needed.

## 6. FAQ

| Symptom | Cause and fix |
|---|---|
| Picture shows infinite recursion in the browser | Self-test captured the monitor displaying the page; verify with two machines |
| Video freezes/artifacts and does not recover | Missing keyframe: click "request keyframe" on the client or reconnect; lower the resolution when packet loss is high |
| Keyboard/mouse do nothing | The target window has a higher integrity level than the injecting process (UIPI, see §3); or the video area was not clicked to take focus |
| `nativecore.dll not found` | Build `server/cpp/nativecore` first (CMake), or use the mss/GDI fallback |
| Coordinates offset with dual monitors | `MOVE` normalized coordinates are relative to the capture source size; injection converts via virtual screen SM_X/YVIRTUALSCREEN — changing monitor order requires a reconnect |
| Firewall blocks everything | Allow the TCP signaling port inbound; WebRTC media ports are negotiated by ICE — corporate networks may need UDP allowed or a TURN server configured |
| Chinese input garbled | TEXT events carry UTF-8 bytes and the injector already converts UTF-8 to Unicode key events; for IME composition prefer the system IME (planned v2 improvement) |