#include "cloudrender/wire/cr_wire.hpp"

#include <cstring>
#include <stdexcept>

namespace cr::wire {
namespace {

// ---- 小端写入 ----
void PutU8(std::vector<uint8_t>& b, uint8_t v) { b.push_back(v); }

void PutU16(std::vector<uint8_t>& b, uint16_t v) {
  b.push_back(static_cast<uint8_t>(v & 0xFF));
  b.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

void PutU32(std::vector<uint8_t>& b, uint32_t v) {
  b.push_back(static_cast<uint8_t>(v & 0xFF));
  b.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
  b.push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
  b.push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

void PutU64(std::vector<uint8_t>& b, uint64_t v) {
  for (int i = 0; i < 8; ++i) b.push_back(static_cast<uint8_t>((v >> (8 * i)) & 0xFF));
}

void PutI16(std::vector<uint8_t>& b, int16_t v) { PutU16(b, static_cast<uint16_t>(v)); }

void PutF32(std::vector<uint8_t>& b, float v) {
  uint32_t bits;
  std::memcpy(&bits, &v, 4);
  PutU32(b, bits);
}

void PutBytes(std::vector<uint8_t>& b, const uint8_t* p, size_t n) {
  b.insert(b.end(), p, p + n);
}

// ---- 小端读取(调用方保证长度) ----
uint16_t GetU16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t GetU32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}
uint64_t GetU64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; --i) v = (v << 8) | p[i];
  return v;
}
int16_t GetI16(const uint8_t* p) { return static_cast<int16_t>(GetU16(p)); }
float GetF32(const uint8_t* p) {
  float v;
  uint32_t bits = GetU32(p);
  std::memcpy(&v, &bits, 4);
  return v;
}

constexpr size_t kHeaderSize = 10;  // magic 4 + ver 1 + seq 4 + n 1
constexpr size_t kEventHeadSize = 9;  // kind 1 + ts 8

// ---- 键码表(W3C code → Windows VK,与 Python protocol.py 全量一致) ----
struct KeyMaps {
  std::unordered_map<std::string, uint16_t> code_to_vk;
  std::unordered_map<uint32_t, std::string> fnv_to_code;
};

const KeyMaps& GetKeyMaps() {
  static const KeyMaps maps = [] {
    KeyMaps m;
    for (int i = 0; i < 26; ++i)
      m.code_to_vk[std::string("Key") + static_cast<char>('A' + i)] =
          static_cast<uint16_t>(0x41 + i);
    for (int i = 0; i < 10; ++i)
      m.code_to_vk["Digit" + std::to_string(i)] = static_cast<uint16_t>(0x30 + i);
    for (int i = 1; i <= 12; ++i)
      m.code_to_vk["F" + std::to_string(i)] = static_cast<uint16_t>(0x70 + i - 1);

    const std::pair<const char*, uint16_t> extras[] = {
        {"Backquote", 0xC0},      {"Minus", 0xBD},         {"Equal", 0xBB},
        {"Backspace", 0x08},      {"Tab", 0x09},           {"BracketLeft", 0xDB},
        {"BracketRight", 0xDD},   {"Backslash", 0xDC},     {"CapsLock", 0x14},
        {"Semicolon", 0x0BA},     {"Quote", 0xDE},         {"Enter", 0x0D},
        {"ShiftLeft", 0xA0},      {"ShiftRight", 0xA1},    {"Comma", 0x0BC},
        {"Period", 0xBE},         {"Slash", 0xBF},         {"ControlLeft", 0xA2},
        {"ControlRight", 0xA3},   {"AltLeft", 0xA4},       {"AltRight", 0xA5},
        {"MetaLeft", 0x5B},       {"MetaRight", 0x5C},     {"Space", 0x20},
        {"ContextMenu", 0x5D},    {"Escape", 0x1B},        {"ArrowUp", 0x26},
        {"ArrowDown", 0x28},      {"ArrowLeft", 0x25},     {"ArrowRight", 0x27},
        {"Home", 0x24},           {"End", 0x23},           {"PageUp", 0x21},
        {"PageDown", 0x22},       {"Insert", 0x2D},        {"Delete", 0x2E},
        {"Numpad0", 0x60},        {"Numpad1", 0x61},       {"Numpad2", 0x62},
        {"Numpad3", 0x63},        {"Numpad4", 0x64},       {"Numpad5", 0x65},
        {"Numpad6", 0x66},        {"Numpad7", 0x67},       {"Numpad8", 0x68},
        {"Numpad9", 0x69},        {"NumpadAdd", 0x6B},     {"NumpadSubtract", 0x6D},
        {"NumpadMultiply", 0x6A}, {"NumpadDivide", 0x6F},  {"NumpadDecimal", 0x6E},
        {"NumpadEnter", 0x0D},    {"NumLock", 0x90},       {"ScrollLock", 0x91},
        {"Pause", 0x13},          {"PrintScreen", 0x2C},
    };
    for (const auto& kv : extras) m.code_to_vk[kv.first] = kv.second;

    for (const auto& kv : m.code_to_vk) m.fnv_to_code.emplace(Fnv1a32(kv.first), kv.first);
    return m;
  }();
  return maps;
}

}  // namespace

// ----------------------------------------------------------------------
uint32_t Fnv1a32(std::string_view s) {
  uint32_t h = 0x811C9DC5u;
  for (unsigned char c : s) {
    h ^= c;
    h = h * 0x01000193u;  // 溢出即 mod 2^32
  }
  return h;
}

uint16_t CodeToVk(std::string_view code) {
  const auto& map = GetKeyMaps().code_to_vk;
  auto it = map.find(std::string(code));
  return it == map.end() ? 0 : it->second;
}

const std::unordered_map<uint32_t, std::string>& FnvToCode() {
  return GetKeyMaps().fnv_to_code;
}

// ----------------------------------------------------------------------
InputEvent InputEvent::Key(bool down, std::string_view code, uint8_t mods,
                           uint8_t repeat, uint64_t ts_ms) {
  InputEvent e{kind::kKey, ts_ms, {}};
  PutU8(e.body, down ? 1 : 0);
  PutU32(e.body, Fnv1a32(code));
  PutU8(e.body, mods);
  PutU8(e.body, repeat);
  return e;
}

InputEvent InputEvent::MouseMove(float x, float y, int16_t dx, int16_t dy,
                                 uint64_t ts_ms) {
  InputEvent e{kind::kMouseMove, ts_ms, {}};
  PutF32(e.body, x);
  PutF32(e.body, y);
  PutI16(e.body, dx);
  PutI16(e.body, dy);
  return e;
}

InputEvent InputEvent::MouseButton(uint8_t button, bool down, uint8_t clicks,
                                   uint64_t ts_ms) {
  InputEvent e{kind::kMouseButton, ts_ms, {}};
  PutU8(e.body, button);
  PutU8(e.body, down ? 1 : 0);
  PutU8(e.body, clicks);
  return e;
}

InputEvent InputEvent::Wheel(float dx, float dy, bool ctrl, uint64_t ts_ms) {
  InputEvent e{kind::kWheel, ts_ms, {}};
  PutF32(e.body, dx);
  PutF32(e.body, dy);
  PutU8(e.body, ctrl ? 1 : 0);
  return e;
}

InputEvent InputEvent::Text(std::string_view text, uint64_t ts_ms) {
  InputEvent e{kind::kText, ts_ms, {}};
  PutU16(e.body, static_cast<uint16_t>(text.size()));
  PutBytes(e.body, reinterpret_cast<const uint8_t*>(text.data()), text.size());
  return e;
}

InputEvent InputEvent::Gamepad(uint8_t index, uint32_t buttons, uint8_t dpad,
                               std::array<float, 6> axes, uint64_t ts_ms) {
  InputEvent e{kind::kGamepad, ts_ms, {}};
  PutU8(e.body, index);
  PutU8(e.body, dpad);
  PutU32(e.body, buttons);
  for (float a : axes) PutF32(e.body, a);
  return e;
}

InputEvent InputEvent::Touch(uint8_t id, uint8_t phase, float x, float y, float pressure,
                             uint64_t ts_ms) {
  InputEvent e{kind::kTouch, ts_ms, {}};
  PutU8(e.body, id);
  PutU8(e.body, phase);
  PutF32(e.body, x);
  PutF32(e.body, y);
  PutF32(e.body, pressure);
  return e;
}

InputEvent InputEvent::Custom(uint8_t plugin_id, const uint8_t* payload, size_t len,
                              uint64_t ts_ms) {
  InputEvent e{kind::kCustom, ts_ms, {}};
  PutU8(e.body, plugin_id);
  PutU16(e.body, static_cast<uint16_t>(len));
  PutBytes(e.body, payload, len);
  return e;
}

// ----------------------------------------------------------------------
std::vector<uint8_t> EncodeInputFrame(const std::vector<InputEvent>& events,
                                      uint32_t seq) {
  size_t n = events.size() < 255 ? events.size() : 255;
  std::vector<uint8_t> out;
  out.reserve(kHeaderSize + n * 32);
  PutBytes(out, reinterpret_cast<const uint8_t*>(kMagicInput), 4);
  PutU8(out, kVersion);
  PutU32(out, seq);
  PutU8(out, static_cast<uint8_t>(n));
  for (size_t i = 0; i < n; ++i) {
    const InputEvent& e = events[i];
    PutU8(out, e.kind);
    PutU64(out, e.ts_ms);
    PutBytes(out, e.body.data(), e.body.size());
  }
  return out;
}

bool DecodeInputFrame(const uint8_t* data, size_t size, uint32_t* seq,
                      std::vector<InputEvent>* events) {
  if (data == nullptr || size < kHeaderSize ||
      std::memcmp(data, kMagicInput, 4) != 0 || data[4] != kVersion)
    return false;
  size_t off = kHeaderSize;
  size_t n = data[9];
  const size_t end = size;
  std::vector<InputEvent> parsed;
  parsed.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    if (off + kEventHeadSize > end) return false;
    uint8_t kind = data[off];
    uint64_t ts = GetU64(data + off + 1);
    off += kEventHeadSize;

    size_t body_len = 0;
    switch (kind) {
      case kind::kKey: body_len = 7; break;
      case kind::kMouseMove: body_len = 12; break;
      case kind::kMouseButton: body_len = 3; break;
      case kind::kWheel: body_len = 9; break;
      case kind::kGamepad: body_len = 30; break;
      case kind::kTouch: body_len = 14; break;
      case kind::kText:
        if (off + 2 > end) return false;
        body_len = 2 + GetU16(data + off);
        break;
      case kind::kCustom:
        if (off + 3 > end) return false;
        body_len = 3 + GetU16(data + off + 1);
        break;
      default:
        return false;  // 未知 kind
    }
    if (off + body_len > end) return false;
    parsed.push_back(InputEvent{kind, ts,
                                std::vector<uint8_t>(data + off, data + off + body_len)});
    off += body_len;
  }
  if (seq != nullptr) *seq = GetU32(data + 5);
  if (events != nullptr) *events = std::move(parsed);
  return true;
}

// ----------------------------------------------------------------------
std::optional<KeyData> AsKey(const InputEvent& e) {
  if (e.kind != kind::kKey || e.body.size() != 7) return std::nullopt;
  return KeyData{e.body[0] != 0, GetU32(e.body.data() + 1), e.body[5], e.body[6]};
}

std::optional<MoveData> AsMouseMove(const InputEvent& e) {
  if (e.kind != kind::kMouseMove || e.body.size() != 12) return std::nullopt;
  return MoveData{GetF32(e.body.data()), GetF32(e.body.data() + 4),
                  GetI16(e.body.data() + 8), GetI16(e.body.data() + 10)};
}

std::optional<ButtonData> AsMouseButton(const InputEvent& e) {
  if (e.kind != kind::kMouseButton || e.body.size() != 3) return std::nullopt;
  return ButtonData{e.body[0], e.body[1] != 0, e.body[2]};
}

std::optional<WheelData> AsWheel(const InputEvent& e) {
  if (e.kind != kind::kWheel || e.body.size() != 9) return std::nullopt;
  return WheelData{GetF32(e.body.data()), GetF32(e.body.data() + 4), e.body[8] != 0};
}

std::optional<GamepadData> AsGamepad(const InputEvent& e) {
  if (e.kind != kind::kGamepad || e.body.size() != 30) return std::nullopt;
  GamepadData d{e.body[0], e.body[1], GetU32(e.body.data() + 2), {}};
  for (int i = 0; i < 6; ++i) d.axes[i] = GetF32(e.body.data() + 6 + 4 * i);
  return d;
}

std::optional<TouchData> AsTouch(const InputEvent& e) {
  if (e.kind != kind::kTouch || e.body.size() != 14) return std::nullopt;
  return TouchData{e.body[0], e.body[1], GetF32(e.body.data() + 2),
                   GetF32(e.body.data() + 6), GetF32(e.body.data() + 10)};
}

std::optional<std::string> AsText(const InputEvent& e) {
  if (e.kind != kind::kText || e.body.size() < 2) return std::nullopt;
  size_t len = GetU16(e.body.data());
  if (e.body.size() != 2 + len) return std::nullopt;
  return std::string(reinterpret_cast<const char*>(e.body.data() + 2), len);
}

std::optional<CustomData> AsCustom(const InputEvent& e) {
  if (e.kind != kind::kCustom || e.body.size() < 3) return std::nullopt;
  size_t len = GetU16(e.body.data() + 1);
  if (e.body.size() != 3 + len) return std::nullopt;
  return CustomData{e.body[0],
                    std::string(reinterpret_cast<const char*>(e.body.data() + 3), len)};
}

// ----------------------------------------------------------------------
std::vector<uint8_t> EvPack(uint8_t ev_kind, const uint8_t* body, size_t body_len,
                            uint64_t ts_ms) {
  std::vector<uint8_t> out;
  out.reserve(kEventHeadSize + body_len);
  PutU8(out, ev_kind);
  PutU64(out, ts_ms);
  PutBytes(out, body, body_len);
  return out;
}

std::vector<uint8_t> EvCursorPos(float x, float y, uint64_t ts_ms) {
  std::vector<uint8_t> body;
  PutF32(body, x);
  PutF32(body, y);
  return EvPack(evt::kCursorPos, body.data(), body.size(), ts_ms);
}

std::vector<uint8_t> EvQuality(uint32_t bitrate_kbps, uint8_t fps, uint64_t ts_ms) {
  std::vector<uint8_t> body;
  PutU32(body, bitrate_kbps);
  PutU8(body, fps);
  return EvPack(evt::kQuality, body.data(), body.size(), ts_ms);
}

std::vector<uint8_t> EvToast(std::string_view text, uint64_t ts_ms) {
  std::vector<uint8_t> body;
  PutU16(body, static_cast<uint16_t>(text.size()));
  PutBytes(body, reinterpret_cast<const uint8_t*>(text.data()), text.size());
  return EvPack(evt::kToast, body.data(), body.size(), ts_ms);
}

std::vector<uint8_t> EncodeEventFrame(const std::vector<std::vector<uint8_t>>& events,
                                      uint32_t seq) {
  size_t n = events.size() < 255 ? events.size() : 255;
  size_t total = kHeaderSize;
  for (size_t i = 0; i < n; ++i) total += events[i].size();
  std::vector<uint8_t> out;
  out.reserve(total);
  PutBytes(out, reinterpret_cast<const uint8_t*>(kMagicEvent), 4);
  PutU8(out, kVersion);
  PutU32(out, seq);
  PutU8(out, static_cast<uint8_t>(n));
  for (size_t i = 0; i < n; ++i) PutBytes(out, events[i].data(), events[i].size());
  return out;
}

}  // namespace cr::wire