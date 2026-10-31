# CloudRender C++ server (8081)

The production CloudRender server. A small signaling shell hosts the built-in Web UI and bridges JSON signaling, while the media session, desktop capture, system audio and input injection all run inside `cloudrender_session.dll` (the C++ session core, see [server/cpp/sdk](../sdk/README.md)). The release package bundles the runtime, so the target machine does not need Python installed.

## Directory contents

```
server/cpp/shell/
├── cpp_server.py        # entry: aiohttp signaling shell (CLI below)
├── cpp_session.py       # CppPeerSession: async wrapper of the cloudrender_session.dll session
├── capi.py              # ctypes bindings for the DLL (load probing + cr_session_poll_signal polling)
├── secure_ops.py        # lock-screen/secure-desktop helpers (lock workstation, secure desktop detection, UIPI hints)
├── protocol.py          # protocol layer: signaling constants, input/event frame codec
├── webui.py             # built-in Web UI hosting
├── wdesktop.py          # secure-desktop worker client
├── wdesktop_worker.py   # secure-desktop worker itself
├── winlogon.py          # elevated worker spawn
├── requirements.txt     # source-mode dependencies (aiohttp + numpy)
└── _wdesktop_worker_cpp.log   # lock-screen worker runtime log (generated at run time)
```

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
pip install -r requirements.txt   # source mode, first run
python cpp_server.py --port 8081
# open http://localhost:8081/ in a browser (built-in Web UI)
# the page Token box is pre-filled with 123456, the same default as --token
```

Admin one-click script `tools\_restart_cpp_server_admin.cmd`: self-elevates → stops any old 8081 instance →
applies staged DLLs (build_sw→build) → starts with `--port 8081 --fps 60 --bitrate 10000`;
console output is redirected to `tools/_cpp_server_console.log`.

When 8081 runs the frozen release (`dist\cloudrender-desktop-cpp-v*-win64`), use `tools\_apply_cpp_release_admin.cmd`
instead: it self-elevates, stops the packaged server, copies `dist\_build\cpp` over the deployed folder and
restarts it (build first with `powershell -File tools\release\build_release.ps1`).

> Administrator rights are for the lock-screen worker (spawning via the winlogon token needs SeDebugPrivilege);
> it also starts without them, but the lock screen degrades to a still frame.

## CLI options

| Option | Default | Description |
|---|---|---|
| `--host` | 0.0.0.0 | listen address |
| `--port` | 8081 | signaling/page port |
| `--token` | 123456 | connection auth token (`ws?token=...`); an empty string disables the check |
| `--max-sessions` | 1 | concurrent session limit (nativecore capture cannot share one monitor) |
| `--fps` | 30 | frame rate cap, 1..60 |
| `--bitrate` | 10000 | video target bitrate kbps (fixed; 0 = adaptive) |
| `--monitor` | -1 | capture monitor index (-1 = primary) |
| `--window` | 0 | capture window handle (e.g. 0x1A2B3C); non-zero switches to window capture |
| `--ice` | none | STUN server (repeatable, e.g. --ice stun:host:3478); default = direct LAN |
| `--dll` | auto | cloudrender_session.dll path (auto-detected build output by default) |
| `--wdesktop-port` | 45995 | secure-desktop worker port |

## Session flow

The browser and the shell use the same signaling flow as documented in [docs/protocol.md](../../../docs/protocol.md):
connect → connected → ready → offer/answer/ice. The DLL queues ICE/closed events and the shell polls them
through `capi.py`; media, capture and injection stay inside the C++ core.