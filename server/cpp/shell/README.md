# CloudRender C++ server signaling shell (8081)

The Python signaling shell around `cloudrender_session.dll` (the C++ session core, see [server/cpp/sdk](../sdk/README.md)):
the signaling protocol and Web UI are completely identical to 8080 (aiortc edition); the media session, capture and injection all happen inside the DLL,
and this process only bridges signaling and governs sessions. **Fully self-contained**: it does not depend on the `server/python` cloudrender package.

## Directory contents

```
server/cpp/shell/
├── cpp_server.py        # 8081 entry: aiohttp signaling shell (CLI below)
├── cpp_session.py       # CppPeerSession: async wrapper of the cloudrender_session.dll session
├── capi.py              # ctypes bindings for the DLL (load probing + cr_session_poll_signal polling)
├── secure_ops.py        # lock-screen/secure-desktop helpers (lock workstation, secure desktop detection, UIPI hints)
├── protocol.py          # protocol layer (synced copy of the file in the cloudrender package)
├── webui.py             # built-in Web UI hosting (same as above)
├── wdesktop.py          # secure-desktop worker client (same as above)
├── wdesktop_worker.py   # secure-desktop worker itself (same as above)
├── winlogon.py          # elevated worker spawn (same as above)
└── _wdesktop_worker_cpp.log   # lock-screen worker runtime log (generated at run time)
```

> Modules sharing names with `server/python/cloudrender/` (protocol/webui/wdesktop/wdesktop_worker/winlogon)
> are mirror copies — any shared-logic change must be applied in both places; secure_ops.py is a shell-specific standalone implementation.

## Prerequisite: build the DLL

The media core `cloudrender_session.dll` must be built first (see [sdk README](../sdk/README.md)):

```bat
cmake -S server\cpp\sdk -B server\cpp\sdk\build -DCR_BUILD_WEBRTC=ON
cmake --build server\cpp\sdk\build --config Release
```

DLL load probing order (see capi.py):

1. Environment variable `CLOUDRENDER_SESSION_DLL` (file path or directory);
2. `server/cpp/sdk/build/{Release,Debug}/cloudrender_session.dll`.

`libwebrtc.dll` and `nativecore.dll` are deployed next to the DLL automatically by the sdk's POST_BUILD step.

## Launch

```bash
cd server/cpp/shell
python cpp_server.py --port 8081
# open http://localhost:8081/ in a browser (built-in Web UI, same page as the 8080 edition)
```

Admin one-click script `tools\_restart_cpp_server_admin.cmd`: self-elevates → stops any old 8081 instance →
applies staged DLLs (build_sw→build) → starts with `--port 8081 --fps 60 --bitrate 10000`;
console output is redirected to `tools/_cpp_server_console.log`.

> Administrator rights are for the lock-screen worker (spawning via the winlogon token needs SeDebugPrivilege);
> it also starts without them, but the lock screen degrades to a still frame.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | 0.0.0.0 | listen address |
| `--port` | 8080 | signaling port (the real deployment uses 8081 to distinguish it from the aiortc edition) |
| `--token` | none | connection auth token (no check by default) |
| `--max-sessions` | 1 | concurrent session limit (nativecore capture cannot share one monitor) |
| `--fps` | 30 | frame rate cap, 1..60 |
| `--bitrate` | 10000 | video target bitrate kbps (fixed; 0 = adaptive) |
| `--monitor` | -1 | capture monitor index (-1 = primary) |
| `--window` | 0 | capture window handle (e.g. 0x1A2B3C); non-zero switches to window capture |
| `--ice` | none | STUN server (repeatable, e.g. --ice stun:host:3478); default = direct LAN |
| `--dll` | auto | cloudrender_session.dll path (auto-detected build output by default) |
| `--wdesktop-port` | 45995 | secure-desktop worker port (isolated from 45990 used by the aiortc edition) |

## Relation to 8080 (aiortc edition)

| Aspect | 8080 Python edition | 8081 this shell |
|---|---|---|
| Entry | `server/python`: `python -m cloudrender.server` | `server/cpp/shell`: `python cpp_server.py --port 8081` |
| Media core | aiortc (Python implementation) | `cloudrender_session.dll` (libwebrtc C++, signal polling) |
| Capture/inject | nativecore via ctypes, mss fallback | nativecore direct (through the DLL) |
| Signaling/page | same protocol (docs/protocol.md) and the same Web UI | identical |
| Lock-screen worker port | 45990 | 45995 |

The two editions can run simultaneously (ports and worker ports are isolated); the browser interface is identical: connect → connected → ready → offer/answer/ice loop.