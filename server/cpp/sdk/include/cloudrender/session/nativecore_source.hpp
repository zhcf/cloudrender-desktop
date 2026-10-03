/**
 * nativecore 直连适配(C++ SDK 零拷贝路径):
 *  NativeCoreFrameSource —— DXGI Desktop Duplication(cr_capture_*)
 *  NativeCoreInjector    —— SendInput 注入(cr_inject_*)
 *
 * 需要 nativecore 头文件路径(#include "cloudrender/capi/cr_api.h")与动态库。
 */
#pragma once

#include <cstdint>
#include <memory>
#include <mutex>

#include "cloudrender/capi/cr_api.h"
#include "cloudrender/session/cr_source.hpp"

namespace cr::source {

class NativeCoreFrameSource : public FrameSource {
 public:
  /// @param monitor 显示器索引;负值 = 主显示器。
  /// @param window_hwnd 非 0 时捕获该窗口所在显示器并按窗口区域裁剪(优先)。
  explicit NativeCoreFrameSource(int monitor = -1, int max_fps = 30,
                                 uint64_t window_hwnd = 0);
  ~NativeCoreFrameSource() override;

  NativeCoreFrameSource(const NativeCoreFrameSource&) = delete;
  NativeCoreFrameSource& operator=(const NativeCoreFrameSource&) = delete;

  int width() const override { return width_; }
  int height() const override { return height_; }
  bool Start() override;
  void Stop() override;
  bool TryTakeLatest(Frame* out) override;

 private:
  static void CR_CALL OnFrameCb(void* user, const cr_frame* frame);

  int monitor_;
  int max_fps_;
  uint64_t window_hwnd_;
  cr_capture* cap_ = nullptr;
  int width_ = 0;
  int height_ = 0;
  std::mutex mu_;
  Frame latest_;
  bool has_latest_ = false;
};

class NativeCoreInjector : public InputInjector {
 public:
  NativeCoreInjector();
  ~NativeCoreInjector() override;

  NativeCoreInjector(const NativeCoreInjector&) = delete;
  NativeCoreInjector& operator=(const NativeCoreInjector&) = delete;

  bool Key(uint16_t vk, bool down) override;
  bool MouseMove(int x, int y) override;
  bool MouseMoveRel(int dx, int dy) override;
  bool MouseButton(int button, bool down) override;
  bool Wheel(int delta) override;
  bool Text(std::string_view utf8) override;

 private:
  cr_inject* inj_ = nullptr;
  std::mutex mu_;  // cr_inject 内部无锁,串行化注入调用
};

}  // namespace cr::source