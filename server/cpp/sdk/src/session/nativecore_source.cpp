#include "cloudrender/session/nativecore_source.hpp"

namespace cr::source {

// ----------------------------------------------------------------------
NativeCoreFrameSource::NativeCoreFrameSource(int monitor, int max_fps,
                                             uint64_t window_hwnd)
    : monitor_(monitor), max_fps_(max_fps), window_hwnd_(window_hwnd) {}

NativeCoreFrameSource::~NativeCoreFrameSource() { Stop(); }

bool NativeCoreFrameSource::Start() {
  if (cap_ != nullptr) return true;
  cap_ = cr_capture_create();
  if (cap_ == nullptr) return false;
  cr_capture_set_frame_callback(cap_, &NativeCoreFrameSource::OnFrameCb, this);
  cr_capture_set_max_fps(cap_, max_fps_);
  // set_target 必须在 start 前:nativecore 不做隐式初始化(与 Python 实现一致)。
  // hwnd 优先(monitor<0 时由 nativecore 按窗口解析显示器);无 hwnd 时 -1 → 主显示器(0)。
  const int32_t target_monitor =
      monitor_ >= 0 ? monitor_ : (window_hwnd_ != 0 ? -1 : 0);
  if (cr_capture_set_target(cap_, target_monitor, window_hwnd_) != CR_OK) {
    cr_capture_destroy(cap_);
    cap_ = nullptr;
    return false;
  }
  if (cr_capture_start(cap_) != CR_OK) {
    cr_capture_destroy(cap_);
    cap_ = nullptr;
    return false;
  }
  int32_t w = 0, h = 0;
  if (cr_capture_get_size(cap_, &w, &h) != CR_OK || w <= 0 || h <= 0) {
    cr_capture_stop(cap_);
    cr_capture_destroy(cap_);
    cap_ = nullptr;
    return false;
  }
  width_ = static_cast<int>(w);
  height_ = static_cast<int>(h);
  return true;
}

void NativeCoreFrameSource::Stop() {
  cr_capture* cap = nullptr;
  {
    std::lock_guard<std::mutex> lock(mu_);
    cap = cap_;
    cap_ = nullptr;  // 先置空:OnFrameCb 见到 nullptr 丢弃尾部帧
    has_latest_ = false;
  }
  if (cap == nullptr) return;
  // 不可持 mu_ 调用:cr_capture_stop 会 join 捕获线程,而捕获线程的
  // OnFrameCb 回调需要 mu_,持锁 join 会死锁(close 时偶发卡死)。
  cr_capture_stop(cap);
  cr_capture_destroy(cap);
}

bool NativeCoreFrameSource::TryTakeLatest(Frame* out) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_latest_) return false;
  *out = std::move(latest_);
  has_latest_ = false;
  return true;
}

void CR_CALL NativeCoreFrameSource::OnFrameCb(void* user, const cr_frame* frame) {
  auto* self = static_cast<NativeCoreFrameSource*>(user);
  if (self == nullptr || frame == nullptr || frame->data == nullptr) return;
  size_t bytes = static_cast<size_t>(frame->stride) * frame->height;
  if (bytes == 0) return;
  std::lock_guard<std::mutex> lock(self->mu_);
  if (self->cap_ == nullptr) return;  // 已 Stop:丢弃尾部帧
  self->latest_.width = static_cast<int>(frame->width);
  self->latest_.height = static_cast<int>(frame->height);
  self->latest_.stride = static_cast<int>(frame->stride);
  self->latest_.pts_us = frame->pts_us;
  self->latest_.bgra.assign(frame->data, frame->data + bytes);
  self->has_latest_ = true;  // 丢旧帧
}

// ----------------------------------------------------------------------
NativeCoreInjector::NativeCoreInjector() { inj_ = cr_inject_create(); }

NativeCoreInjector::~NativeCoreInjector() {
  std::lock_guard<std::mutex> lock(mu_);
  if (inj_ != nullptr) {
    cr_inject_destroy(inj_);
    inj_ = nullptr;
  }
}

bool NativeCoreInjector::Key(uint16_t vk, bool down) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr && cr_inject_key(inj_, vk, down ? 1 : 0) == CR_OK;
}

bool NativeCoreInjector::MouseMove(int x, int y) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr && cr_inject_mouse_move(inj_, x, y) == CR_OK;
}

bool NativeCoreInjector::MouseMoveRel(int dx, int dy) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr && cr_inject_mouse_move_rel(inj_, dx, dy) == CR_OK;
}

bool NativeCoreInjector::MouseButton(int button, bool down) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr && cr_inject_mouse_button(inj_, button, down ? 1 : 0) == CR_OK;
}

bool NativeCoreInjector::Wheel(int delta) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr && cr_inject_mouse_wheel(inj_, delta) == CR_OK;
}

bool NativeCoreInjector::Text(std::string_view utf8) {
  std::lock_guard<std::mutex> lock(mu_);
  return inj_ != nullptr &&
         cr_inject_text(inj_, std::string(utf8).c_str()) == CR_OK;  // API 要求 NUL 结尾
}

}  // namespace cr::source