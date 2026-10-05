# CloudRender Python SDK (server side)

An aiortc-based general cloud desktop server SDK: capture the local desktop → stream to the browser over WebRTC → inject keyboard/mouse back. The single protocol implementation lives in `cloudrender/protocol.py`.

## Install

```bash
pip install -r requirements.txt     # aiohttp + aiortc + numpy + mss
```

## Quick start (local cloud desktop demo)

```bash
cd server/python
python -m cloudrender.server --port 8080
# open http://localhost:8080/ in a browser (built-in Web UI; the page auto-connects to the same-port signaling)
```

> The entry point must run with `-m` (the package uses relative imports throughout); `python cloudrender\server.py` does not work.

The server listens on 0.0.0.0:8080 by default; from another computer on the LAN open `http://<LAN-IP>:8080/`
(find the IPv4 with ipconfig, e.g. 192.168.1.105) and it just works (page and signaling share the port); if it does not connect,
check whether the local Windows Firewall allows inbound 8080.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | 0.0.0.0 | listen address |
| `--port` | 8080 | signaling and Web UI port |
| `--monitor` | 0 | 0=entire desktop (full virtual screen), 1=primary monitor, 2..=other monitors |
| `--fps` | 30 | frame rate cap |
| `--bitrate` | 6000 | video target bitrate kbps; too low looks blurry — LAN can go 8000-12000 |
| `--token` | none | optional auth (`ws?token=...`) |
| `--max-sessions` | 1 | concurrent session limit (nativecore capture cannot share one monitor) |
| `--capture` | auto | auto=DXGI zero-copy preferred for single monitors, fall back to mss on failure/multi-monitor; mss=force GDI software capture; dxgi=force DXGI |
| `--force-fallback` | - | equivalent to `--capture mss` (no nativecore/DXGI) |

Headless (no physical monitor) operation: install/enable a virtual display driver first (IddCx class, resolution 1920x1080); startup detects the monitor count and prints a hint.

## Package structure and layering (`cloudrender/`)

Modules fall into two layers — the **SDK layer** (independently integrable session/capture capabilities) and the **application layer** (the server process itself) — living in one self-contained unit, not split physically; the mapping to `cpp/sdk` + `cpp/shell` on the C++ side is below.

```
cloudrender/
├── server.py           # CLI entry + CloudRenderServer (aiohttp signaling + session orchestration, merged)
├── protocol.py         # single protocol implementation: signaling/input/event frame codec, FNV key table
├── capture.py          # FrameSource abstraction; MssScreenSource / NativeCoreCaptureSource (ctypes)
├── inject.py           # InputInjector abstraction; WindowsInputInjector (SendInput) / NativeCoreInjector
├── session.py          # PeerSession: aiortc media session (offer/answer/ice, data channels)
├── streamer.py         # CaptureVideoTrack: frame pump (latest frame -> encode & push)
├── webui.py            # built-in Web UI: mounts client/web and client/javascript/src into aiohttp
├── wdesktop.py         # local client of the secure-desktop (lock screen video/input) worker
├── wdesktop_worker.py  # secure-desktop worker itself (spawned with the winlogon token, SYSTEM)
├── winlogon.py         # spawn_wdesktop_worker: elevated spawn (needs admin, SeDebugPrivilege)
├── mini_client.py      # GUI-less protocol verification client (decoded frame/fps stats + CLI injection)
└── __init__.py         # package exports (CloudRenderServer lazily exported for programmatic import)
```

| Layer | Module | C++ counterpart |
|---|---|---|
| SDK layer | `protocol.py` | `cpp/sdk/wire/cr_wire.*` (byte-identical, guarded by contract tests) |
| SDK layer | `capture.py` / `inject.py` | `cpp/sdk/session/`: `cr_source.hpp` + `nativecore_source.*` (nativecore adapters) |
| SDK layer | `session.py` / `streamer.py` | `cpp/sdk/session/cr_webrtc_session.*` (libwebrtc session + frame pump) |
| Application layer | `server.py` | `cpp/shell/cpp_server.py` (same-role signaling shell, media core swapped for cloudrender_session.dll) |
| Application layer | `webui.py` / `wdesktop.py` / `winlogon.py` / `wdesktop_worker.py` | same-named copies in `cpp/shell/` (synced from the same source) |
| Application layer | `mini_client.py` | — (Python side only) |

> The C++ side has an extra C ABI export layer (`cpp/sdk/capi/cr_session_api.h`, loaded by the 8081 shell via ctypes); Python has none — same-process direct import.
> The five modules protocol/webui/wdesktop/winlogon/wdesktop_worker inside `cpp/shell/` are same-source copies of this package; changes must be applied in both places.

## Programmatic usage

```python
from cloudrender import CloudRenderServer, MssScreenSource, WindowsInputInjector

server = CloudRenderServer(port=8080, token=None, max_sessions=1, bitrate=8_000_000)
server.run(
    source_factory=lambda info: MssScreenSource(monitor=0, max_fps=30),
    injector_factory=lambda info: WindowsInputInjector(),
)
```

In cloud desktop mode, swap `MssScreenSource` for `NativeCoreCaptureSource` (nativecore.dll under `server/cpp/nativecore/build/Release/`) to get DXGI zero-copy capture; in engine embedding mode, implement your own `FrameSource` (see docs/integration.md).

## Integration test (mini_client)

```bash
python -m cloudrender.mini_client ws://127.0.0.1:8080/ws
# commands: move <x> <y> | click <button> | key <code> | text <str> | quit
```

## Lock screen (secure desktop)

While the desktop is locked, ordinary processes cannot capture or inject; the server spawns a SYSTEM worker (`wdesktop_worker.py`) with the winlogon token to take over, and the browser can type the lock-screen password directly.
**The server must run as administrator** (SeDebugPrivilege); otherwise it degrades to a still lock-screen frame. Worker log: `server/python/_wdesktop_worker.log`.