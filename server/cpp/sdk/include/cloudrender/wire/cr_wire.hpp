/**
 * CloudRender 统一协议(C++)——与 Python/JS 字节级一致,规范见 docs/protocol.md。
 *
 * 设计要点:
 *  - InputEvent::body 存放"线上字节原样"(事件头 kind+ts 之后的部分)。
 *    工厂函数按线格式构造 body,解码器原样截取 body,
 *    因此 encode(decode(x)) == x 由构造保证,不依赖逐字段打包。
 *  - 本模块仅依赖标准库,可在任意平台编译与单测。
 */
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace cr::wire {

constexpr uint8_t kVersion = 1;
constexpr char kMagicInput[4] = {'C', 'R', 'I', 'N'};
constexpr char kMagicEvent[4] = {'C', 'R', 'E', 'V'};

// ---- 输入事件 kind ----
namespace kind {
constexpr uint8_t kKey = 0x01;
constexpr uint8_t kMouseMove = 0x02;
constexpr uint8_t kMouseButton = 0x03;
constexpr uint8_t kWheel = 0x04;
constexpr uint8_t kGamepad = 0x05;
constexpr uint8_t kTouch = 0x06;
constexpr uint8_t kText = 0x07;
constexpr uint8_t kCustom = 0x7F;
}  // namespace kind

// ---- 事件通道(S→C)kind ----
namespace evt {
constexpr uint8_t kCursorPos = 0x01;
constexpr uint8_t kQuality = 0x02;
constexpr uint8_t kToast = 0x03;
constexpr uint8_t kPong = 0x04;
}  // namespace evt

// ---- FNV-1a/32 与键码 ----
uint32_t Fnv1a32(std::string_view s);

/// W3C KeyboardEvent.code → Windows VK;未知返回 0x00。
uint16_t CodeToVk(std::string_view code);

/// fnv → code 反查表(键盘事件解码用)。
const std::unordered_map<uint32_t, std::string>& FnvToCode();

// ---- 输入事件 ----

struct InputEvent {
  uint8_t kind = 0;
  uint64_t ts_ms = 0;
  std::vector<uint8_t> body;  // 线格式原样(kind/ts 之后的字节)

  static InputEvent Key(bool down, std::string_view code, uint8_t mods = 0,
                        uint8_t repeat = 0, uint64_t ts_ms = 0);
  static InputEvent MouseMove(float x, float y, int16_t dx = 0, int16_t dy = 0,
                              uint64_t ts_ms = 0);
  static InputEvent MouseButton(uint8_t button, bool down, uint8_t clicks = 1,
                                uint64_t ts_ms = 0);
  static InputEvent Wheel(float dx, float dy, bool ctrl = false, uint64_t ts_ms = 0);
  static InputEvent Text(std::string_view text, uint64_t ts_ms = 0);
  static InputEvent Gamepad(uint8_t index, uint32_t buttons, uint8_t dpad = 0,
                            std::array<float, 6> axes = {}, uint64_t ts_ms = 0);
  static InputEvent Touch(uint8_t id, uint8_t phase, float x, float y,
                          float pressure = 1.0f, uint64_t ts_ms = 0);
  static InputEvent Custom(uint8_t plugin_id, const uint8_t* payload, size_t len,
                           uint64_t ts_ms = 0);
};

/// 打包输入帧:magic CRIN + ver + seq + n + events。n 上限 255。
std::vector<uint8_t> EncodeInputFrame(const std::vector<InputEvent>& events,
                                      uint32_t seq = 0);

/// 解析输入帧;非法返回 false(部分写入的 events 内容应忽略)。
bool DecodeInputFrame(const uint8_t* data, size_t size, uint32_t* seq,
                      std::vector<InputEvent>* events);

// ---- 解码访问器(服务端分发用;kind/长度不符返回 nullopt) ----

struct KeyData {
  bool down;
  uint32_t code;  // FNV-1a32(KeyboardEvent.code)
  uint8_t mods;
  uint8_t repeat;
};
std::optional<KeyData> AsKey(const InputEvent& e);

struct MoveData {
  float x, y;   // 归一化 0..1;负值 = 未提供
  int16_t dx, dy;
};
std::optional<MoveData> AsMouseMove(const InputEvent& e);

struct ButtonData {
  uint8_t button;
  bool down;
  uint8_t clicks;
};
std::optional<ButtonData> AsMouseButton(const InputEvent& e);

struct WheelData {
  float dx, dy;
  bool ctrl;
};
std::optional<WheelData> AsWheel(const InputEvent& e);

struct GamepadData {
  uint8_t index;
  uint8_t dpad;
  uint32_t buttons;
  std::array<float, 6> axes;
};
std::optional<GamepadData> AsGamepad(const InputEvent& e);

struct TouchData {
  uint8_t id;
  uint8_t phase;
  float x, y, pressure;
};
std::optional<TouchData> AsTouch(const InputEvent& e);

std::optional<std::string> AsText(const InputEvent& e);

struct CustomData {
  uint8_t plugin_id;
  std::string payload;
};
std::optional<CustomData> AsCustom(const InputEvent& e);

// ---- 事件通道编码(S→C) ----
std::vector<uint8_t> EvPack(uint8_t ev_kind, const uint8_t* body, size_t body_len,
                            uint64_t ts_ms = 0);
std::vector<uint8_t> EvCursorPos(float x, float y, uint64_t ts_ms = 0);
std::vector<uint8_t> EvQuality(uint32_t bitrate_kbps, uint8_t fps, uint64_t ts_ms = 0);
std::vector<uint8_t> EvToast(std::string_view text, uint64_t ts_ms = 0);
std::vector<uint8_t> EncodeEventFrame(const std::vector<std::vector<uint8_t>>& events,
                                      uint32_t seq = 0);

}  // namespace cr::wire