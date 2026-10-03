// CloudRender nativecore - C ABI 实现(捕获句柄 + 注入句柄 + 导出)
// CR_NATIVECORE_BUILD 由 CMake 统一定义(避免与命令行宏重定义冲突)
#include <windows.h>

#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "cloudrender/capi/cr_api.h"
#include "cr_capture.hpp"

namespace cloudrender::core {
// cr_inject.cpp 中的注入原语
int InjectKeyEvent(cr_inject*, uint16_t vk, int down);
int InjectMouseMove(cr_inject*, int32_t x, int32_t y);
int InjectMouseMoveRel(cr_inject*, int32_t dx, int32_t dy);
int InjectMouseButton(cr_inject*, int button, int down);
int InjectMouseWheel(cr_inject*, int32_t delta);
int InjectText(cr_inject*, const char* utf8);
}  // namespace cloudrender::core

using cloudrender::core::DxgiCapture;
using cloudrender::core::WideToUtf8;

namespace {

struct CaptureHandle {
  std::mutex mu;
  DxgiCapture impl;
  cr_frame_cb cb = nullptr;
  void* cb_user = nullptr;
  int max_fps = 30;
  bool cb_set = false;
};

struct InjectHandle {};  // 注入原语无状态,句柄仅作生命周期占位

/* 帧回调:C++ 捕获线程 → C 回调 */
void FrameTrampoline(void* user, const cr_frame* frame) {
  auto* h = static_cast<CaptureHandle*>(user);
  if (h->cb) h->cb(h->cb_user, frame);
}

/* 由窗口句柄解析其所在显示器索引(与 EnumOutputs 顺序对应) */
int32_t MonitorIndexForWindow(HWND hwnd) {
  HMONITOR target = ::MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
  int32_t index = 0;
  bool found = false;
  struct Ctx {
    HMONITOR target;
    int32_t* index;
    bool* found;
  } ctx{target, &index, &found};
  ::EnumDisplayMonitors(
      nullptr, nullptr,
      [](HMONITOR mon, HDC, LPRECT, LPARAM lparam) -> BOOL {
        auto* c = reinterpret_cast<Ctx*>(lparam);
        if (mon == c->target) {
          *c->found = true;
          return FALSE;
        }
        ++*c->index;
        return TRUE;
      },
      reinterpret_cast<LPARAM>(&ctx));
  return found ? index : 0;
}

/* 窗口枚举:仅列出可见且有标题的顶层窗口 */
std::vector<std::pair<HWND, std::string>> EnumerateWindows() {
  std::vector<std::pair<HWND, std::string>> out;
  ::EnumWindows(
      [](HWND hwnd, LPARAM lparam) -> BOOL {
        auto* vec = reinterpret_cast<std::vector<std::pair<HWND, std::string>>*>(lparam);
        if (!::IsWindowVisible(hwnd)) return TRUE;
        if (::GetWindow(hwnd, GW_OWNER)) return TRUE;  // 跳过工具窗口
        const int len = ::GetWindowTextLengthW(hwnd);
        if (len <= 0) return TRUE;
        std::wstring title(static_cast<size_t>(len + 1), L'\0');
        ::GetWindowTextW(hwnd, &title[0], len + 1);
        vec->emplace_back(hwnd, cloudrender::core::WideToUtf8(title.c_str()));
        return TRUE;
      },
      reinterpret_cast<LPARAM>(&out));
  return out;
}

}  // namespace

extern "C" {

/* ---------- 捕获 ---------- */

CR_API cr_capture* cr_capture_create(void) {
  return reinterpret_cast<cr_capture*>(new CaptureHandle());
}

CR_API void cr_capture_destroy(cr_capture* cap) {
  if (!cap) return;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  h->impl.Stop();
  delete h;
}

CR_API int cr_capture_list_monitors(cr_capture* cap, int32_t* buf, int buf_count) {
  if (!cap) return CR_E_ARG;
  int count = ::GetSystemMetrics(SM_CMONITORS);
  if (!buf) return count;
  const int n = count < buf_count ? count : buf_count;
  for (int i = 0; i < n; ++i) buf[i] = i;
  return count;
}

CR_API int cr_capture_list_windows(cr_capture* cap, cr_window_info* buf, int buf_count) {
  if (!cap) return CR_E_ARG;
  const auto windows = EnumerateWindows();
  if (!buf) return static_cast<int>(windows.size());
  const int n = static_cast<int>(windows.size()) < buf_count
                    ? static_cast<int>(windows.size())
                    : buf_count;
  for (int i = 0; i < n; ++i) {
    buf[i].hwnd = reinterpret_cast<uint64_t>(windows[i].first);
    snprintf(buf[i].title, sizeof(buf[i].title), "%s", windows[i].second.c_str());
  }
  return static_cast<int>(windows.size());
}

CR_API int cr_capture_set_target(cr_capture* cap, int32_t monitor, uint64_t window_hwnd) {
  if (!cap) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  if ((monitor < 0) && window_hwnd == 0) return CR_E_ARG;
  int32_t m = monitor;
  if (window_hwnd != 0) {
    if (monitor < 0) m = MonitorIndexForWindow(reinterpret_cast<HWND>(window_hwnd));
  }
  return h->impl.TryInitialize(m, window_hwnd) ? CR_OK : CR_E_SYS;
}

CR_API int cr_capture_set_max_fps(cr_capture* cap, int fps) {
  if (!cap) return CR_E_ARG;
  if (fps < 1 || fps > 60) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  h->max_fps = fps;
  return CR_OK;
}

CR_API int cr_capture_set_frame_callback(cr_capture* cap, cr_frame_cb cb, void* user) {
  if (!cap || !cb) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  h->cb = cb;
  h->cb_user = user;
  h->cb_set = true;
  return CR_OK;
}

CR_API int cr_capture_get_size(cr_capture* cap, int32_t* width, int32_t* height) {
  if (!cap || !width || !height) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  *width = h->impl.Width();
  *height = h->impl.Height();
  return (*width > 0 && *height > 0) ? CR_OK : CR_E_STATE;
}

CR_API int cr_capture_start(cr_capture* cap) {
  if (!cap) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  if (!h->cb_set) return CR_E_STATE;
  return h->impl.Start(FrameTrampoline, h, h->max_fps) ? CR_OK : CR_E_SYS;
}

CR_API int cr_capture_stop(cr_capture* cap) {
  if (!cap) return CR_E_ARG;
  auto* h = reinterpret_cast<CaptureHandle*>(cap);
  h->impl.Stop();
  return CR_OK;
}

/* ---------- 注入 ---------- */

CR_API cr_inject* cr_inject_create(void) {
  return reinterpret_cast<cr_inject*>(new InjectHandle());
}

CR_API void cr_inject_destroy(cr_inject* inj) {
  delete reinterpret_cast<InjectHandle*>(inj);
}

CR_API int cr_inject_key(cr_inject* inj, uint16_t vk, int down) {
  return cloudrender::core::InjectKeyEvent(inj, vk, down);
}

CR_API int cr_inject_mouse_move(cr_inject* inj, int32_t x, int32_t y) {
  return cloudrender::core::InjectMouseMove(inj, x, y);
}

CR_API int cr_inject_mouse_move_rel(cr_inject* inj, int32_t dx, int32_t dy) {
  return cloudrender::core::InjectMouseMoveRel(inj, dx, dy);
}

CR_API int cr_inject_mouse_button(cr_inject* inj, int button, int down) {
  return cloudrender::core::InjectMouseButton(inj, button, down);
}

CR_API int cr_inject_mouse_wheel(cr_inject* inj, int32_t delta) {
  return cloudrender::core::InjectMouseWheel(inj, delta);
}

CR_API int cr_inject_text(cr_inject* inj, const char* utf8) {
  return cloudrender::core::InjectText(inj, utf8);
}

}  // extern "C"