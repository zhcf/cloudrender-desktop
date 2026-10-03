/**
 * CloudRender 帧源/注入抽象(C++)——与 Python `FrameSource` / `InputInjector`
 * 语义一致。
 */
#pragma once

#include <cstdint>
#include <string_view>
#include <vector>

namespace cr::source {

/// 一帧 BGRA32 像素(行字节 = stride)。
struct Frame {
  int width = 0;
  int height = 0;
  int stride = 0;
  uint64_t pts_us = 0;
  std::vector<uint8_t> bgra;
};

/// 捕获源:生产线程推最新帧,会话线程按帧率取走(丢旧帧)。
class FrameSource {
 public:
  virtual ~FrameSource() = default;
  virtual int width() const = 0;
  virtual int height() const = 0;
  /// 启动捕获;失败返回 false(应已配置目标/回调)。
  virtual bool Start() = 0;
  virtual void Stop() = 0;
  /// 取最新一帧;无新帧返回 false。
  virtual bool TryTakeLatest(Frame* out) = 0;
};

/// 输入注入(Windows VK 层面)。
class InputInjector {
 public:
  virtual ~InputInjector() = default;
  virtual bool Key(uint16_t vk, bool down) = 0;
  virtual bool MouseMove(int x, int y) = 0;
  virtual bool MouseMoveRel(int dx, int dy) = 0;
  virtual bool MouseButton(int button, bool down) = 0;
  virtual bool Wheel(int delta) = 0;
  virtual bool Text(std::string_view utf8) = 0;
};

}  // namespace cr::source