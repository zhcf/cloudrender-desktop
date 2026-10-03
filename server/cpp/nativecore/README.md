# CloudRender nativecore(C++ 原生核心)

桌面捕获与输入注入的统一原生核心,C ABI 导出,可被 C++ 直接链接、Python ctypes 及任意语言绑定。

## 能力

- **捕获**:DXGI Desktop Duplication,整显示器 / 指定窗口裁剪,帧内含系统光标(BGRA32,top-down);
- **注入**:SendInput——键盘(VK)、鼠标绝对/相对、按键、滚轮、Unicode 文本;
- **帧交付**:回调推送,零中间缓存(窗口裁剪模式除外)。

## 构建(Windows,MSVC)

```powershell
cd server/cpp/nativecore
cmake -S . -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
# 产物:build/Release/nativecore.dll (及 .lib/.exp);POST_BUILD 另生成
# cloudrender_nativecore.dll/.lib 别名副本(sdk 按该名 find_library)
```

## C ABI 速览(完整见 include/cloudrender/capi/cr_api.h)

```c
cr_capture* cap = cr_capture_create();
cr_capture_set_frame_callback(cap, on_frame, /*user*/NULL); // 回调线程交付,勿阻塞
cr_capture_set_max_fps(cap, 30);
cr_capture_set_target(cap, /*monitor*/0, /*hwnd*/0);        // 或指定窗口
cr_capture_start(cap);
...
cr_capture_stop(cap);
cr_capture_destroy(cap);

cr_inject* inj = cr_inject_create();
cr_inject_key(inj, VK_A, 1);
cr_inject_mouse_move(inj, 640, 480);
cr_inject_mouse_button(inj, 0, 1);   // 左键按下
cr_inject_mouse_wheel(inj, 120);
cr_inject_text(inj, "hello");
```

## 限制与注意(v1)

- 仅 Windows。同一显示器同一时刻只允许一个 Duplication 实例(与其他捕获程序互斥,失败返回 `CR_E_SYS`);
- 窗口裁剪模式:窗口被遮挡时裁剪区域显示遮挡内容(OS 限制);
- 帧率为上限控制,实际受显示器刷新率与内容变化驱动(静止画面不重复出帧,由上层决定是否补帧);
- 注入等同本机键鼠,受 UIPI 限制,不能注入更高完整性级别的窗口(见 docs/integration.md)。