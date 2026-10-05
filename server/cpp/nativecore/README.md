# CloudRender nativecore (C++ native core)

The unified native core for desktop capture and input injection, exported via C ABI; linkable from C++ directly, usable via Python ctypes, or bound from any language.

## Capabilities

- **Capture**: DXGI Desktop Duplication, full monitor / cropped target window, frames include the system cursor (BGRA32, top-down);
- **Injection**: SendInput — keyboard (VK), mouse absolute/relative, buttons, wheel, Unicode text;
- **Frame delivery**: callback push, no intermediate buffering (except in window-crop mode).

## Build (Windows, MSVC)

```powershell
cd server/cpp/nativecore
cmake -S . -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
# Output: build/Release/nativecore.dll (plus .lib/.exp); POST_BUILD also produces
# the cloudrender_nativecore.dll/.lib alias copies (the sdk locates it by that name)
```

## C ABI at a glance (full version: include/cloudrender/capi/cr_api.h)

```c
cr_capture* cap = cr_capture_create();
cr_capture_set_frame_callback(cap, on_frame, /*user*/NULL); // delivered on the callback thread, do not block
cr_capture_set_max_fps(cap, 30);
cr_capture_set_target(cap, /*monitor*/0, /*hwnd*/0);        // or a specific window
cr_capture_start(cap);
...
cr_capture_stop(cap);
cr_capture_destroy(cap);

cr_inject* inj = cr_inject_create();
cr_inject_key(inj, VK_A, 1);
cr_inject_mouse_move(inj, 640, 480);
cr_inject_mouse_button(inj, 0, 1);   // left button down
cr_inject_mouse_wheel(inj, 120);
cr_inject_text(inj, "hello");
```

## Limitations and notes (v1)

- Windows only. Only one Duplication instance per monitor at a time (mutually exclusive with other capture programs; failure returns `CR_E_SYS`);
- Window-crop mode: when the window is occluded, the cropped region shows the occluding content (an OS limitation);
- The frame rate is a cap; actual output is driven by monitor refresh and content changes (a static picture does not repeat frames — the layer above decides whether to duplicate);
- Injection equals local keyboard/mouse and is subject to UIPI; it cannot inject into windows of a higher integrity level (see docs/integration.md).