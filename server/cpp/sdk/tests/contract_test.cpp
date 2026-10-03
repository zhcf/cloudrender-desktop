/**
 * CloudRender 协议契约测试:C++ cr_wire ↔ Python 参考实现
 *
 * 金标准由 tools/golden_wire.py 生成(server/cpp/sdk/tests/golden.txt),三行格式:
 *   INPUT_HEX=<hex>            输入帧(seq=42,9 个事件,8 种 kind 全覆盖)
 *   EVENTS_HEX=<hex>           事件通道帧(seq=7)
 *   FNV_VECTORS=name=value ... FNV-1a32 基准向量(空 name 呈现为 "=value")
 *
 * 本测试只依赖标准库 + cr_wire,任何平台可构建。由 ctest 调用:
 *   add_test(NAME cr_wire_contract COMMAND cr_contract_test <golden.txt>)
 */
#include "cloudrender/wire/cr_wire.hpp"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

namespace {

int g_failures = 0;

void Check(bool ok, const char* name) {
  if (!ok) {
    ++g_failures;
    std::printf("FAIL %s\n", name);
  }
}

std::string ToHex(const std::vector<uint8_t>& b) {
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(b.size() * 2);
  for (uint8_t c : b) {
    out += kHex[c >> 4];
    out += kHex[c & 0x0F];
  }
  return out;
}

void CheckBytes(const char* name, const std::vector<uint8_t>& actual,
                const std::vector<uint8_t>& expected) {
  if (actual != expected) {
    ++g_failures;
    std::printf("FAIL %s\n  actual  : %s\n  expected: %s\n", name, ToHex(actual).c_str(),
                ToHex(expected).c_str());
  }
}

bool FromHex(const std::string& hex, std::vector<uint8_t>* out) {
  if (hex.size() % 2 != 0) return false;
  auto val = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  out->clear();
  out->reserve(hex.size() / 2);
  for (size_t i = 0; i < hex.size(); i += 2) {
    int hi = val(hex[i]), lo = val(hex[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out->push_back(static_cast<uint8_t>((hi << 4) | lo));
  }
  return true;
}

// "KeyA=774562871 Enter=2952291245 =2166136261 你好=2257816995" → (name, value) 列表
std::vector<std::pair<std::string, uint32_t>> ParseFnvLine(const std::string& line) {
  std::vector<std::pair<std::string, uint32_t>> out;
  size_t pos = 0;
  while (pos < line.size()) {
    size_t sp = line.find(' ', pos);
    std::string tok =
        line.substr(pos, sp == std::string::npos ? std::string::npos : sp - pos);
    if (!tok.empty()) {
      size_t eq = tok.find('=');
      if (eq != std::string::npos) {
        std::string name = tok.substr(0, eq);
        uint32_t value =
            static_cast<uint32_t>(std::strtoul(tok.c_str() + eq + 1, nullptr, 10));
        out.emplace_back(std::move(name), value);
      }
    }
    if (sp == std::string::npos) break;
    pos = sp + 1;
  }
  return out;
}

bool Approximately(float actual, float expected) {
  return std::fabs(actual - expected) < 1e-5f;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "用法:cr_contract_test <golden.txt>\n");
    return 2;
  }
  std::ifstream in(argv[1], std::ios::binary);
  if (!in) {
    std::fprintf(stderr, "无法打开金标准文件:%s\n", argv[1]);
    return 2;
  }
  std::string input_line, events_line, fnv_line;
  std::getline(in, input_line);
  std::getline(in, events_line);
  std::getline(in, fnv_line);
  // 剪掉 CRLF 行尾的 '\r'(Windows 上生成的金标准行尾;hex 尾带 '\r' 会破坏偶数长度检查)
  for (std::string* line : {&input_line, &events_line, &fnv_line}) {
    while (!line->empty() && (line->back() == '\r' || line->back() == '\n')) {
      line->pop_back();
    }
  }

  const std::string kInputPrefix = "INPUT_HEX=";
  const std::string kEventsPrefix = "EVENTS_HEX=";
  const std::string kFnvPrefix = "FNV_VECTORS=";
  if (input_line.rfind(kInputPrefix, 0) != 0 || events_line.rfind(kEventsPrefix, 0) != 0 ||
      fnv_line.rfind(kFnvPrefix, 0) != 0) {
    std::fprintf(stderr, "golden.txt 格式错误(应为 INPUT_HEX/EVENTS_HEX/FNV_VECTORS 三行)\n");
    return 2;
  }
  std::vector<uint8_t> golden_input, golden_events;
  if (!FromHex(input_line.substr(kInputPrefix.size()), &golden_input) ||
      !FromHex(events_line.substr(kEventsPrefix.size()), &golden_events)) {
    std::fprintf(stderr, "golden.txt hex 解析失败\n");
    return 2;
  }
  auto fnv_pairs = ParseFnvLine(fnv_line.substr(kFnvPrefix.size()));

  using namespace cr::wire;

  /* ---------- 1. FNV-1a32 基准向量(含空串与非 ASCII) ---------- */
  for (const auto& kv : fnv_pairs)
    Check(Fnv1a32(kv.first) == kv.second, "fnv1a32 vector");

  /* ---------- 2. 输入帧编码(与 Python golden 字节级一致) ---------- */
  const uint8_t* kXyz = reinterpret_cast<const uint8_t*>("xyz");
  std::vector<InputEvent> events;
  events.push_back(InputEvent::Key(true, "KeyA", 2, 1, 123));
  events.push_back(InputEvent::MouseMove(0.5f, 0.25f, -3, 2, 124));
  events.push_back(InputEvent::MouseButton(1, true, 2, 125));
  events.push_back(InputEvent::Wheel(0.0f, 1.0f, true, 126));
  events.push_back(InputEvent::Text("hello", 127));
  events.push_back(
      InputEvent::Gamepad(0, 12, 3, {0.1f, -0.2f, 0.0f, -0.5f, 0.0f, 0.5f}, 128));
  events.push_back(InputEvent::Custom(9, kXyz, 3, 129));
  events.push_back(InputEvent::Text(u8"你好", 130));
  events.push_back(InputEvent::Touch(2, 1, 0.25f, 0.5f, 0.9f, 131));

  CheckBytes("input frame bytes", EncodeInputFrame(events, 42), golden_input);

  /* ---------- 3. 解码往返 + 访问器 ---------- */
  uint32_t seq = 0;
  std::vector<InputEvent> decoded;
  Check(DecodeInputFrame(golden_input.data(), golden_input.size(), &seq, &decoded),
        "golden input decode");
  Check(seq == 42, "decoded seq");
  Check(decoded.size() == events.size(), "decoded event count");
  if (decoded.size() == events.size())
    CheckBytes("round trip encode(decode(golden))", EncodeInputFrame(decoded, seq),
               golden_input);

  if (decoded.size() == 9) {
    auto key = AsKey(decoded[0]);
    Check(key && key->down && key->code == Fnv1a32("KeyA") && key->mods == 2 &&
              key->repeat == 1,
          "AsKey");
    auto mv = AsMouseMove(decoded[1]);
    Check(mv && mv->x == 0.5f && mv->y == 0.25f && mv->dx == -3 && mv->dy == 2,
          "AsMouseMove");
    auto btn = AsMouseButton(decoded[2]);
    Check(btn && btn->button == 1 && btn->down && btn->clicks == 2, "AsMouseButton");
    auto wh = AsWheel(decoded[3]);
    Check(wh && wh->dx == 0.0f && wh->dy == 1.0f && wh->ctrl, "AsWheel");
    auto txt = AsText(decoded[4]);
    Check(txt && *txt == "hello", "AsText ascii");
    auto gp = AsGamepad(decoded[5]);
    Check(gp && gp->index == 0 && gp->dpad == 3 && gp->buttons == 12 &&
              Approximately(gp->axes[0], 0.1f) && Approximately(gp->axes[1], -0.2f) &&
              Approximately(gp->axes[3], -0.5f) && Approximately(gp->axes[5], 0.5f),
          "AsGamepad");
    auto cust = AsCustom(decoded[6]);
    Check(cust && cust->plugin_id == 9 && cust->payload == "xyz", "AsCustom");
    auto txt2 = AsText(decoded[7]);
    Check(txt2 && *txt2 == u8"你好", "AsText utf8");
    auto touch = AsTouch(decoded[8]);
    Check(touch && touch->id == 2 && touch->phase == 1 && touch->x == 0.25f &&
              touch->y == 0.5f && Approximately(touch->pressure, 0.9f),
          "AsTouch");
    Check(!AsKey(decoded[1]).has_value(), "kind mismatch -> nullopt");
  }

  /* ---------- 4. 键码表(与 Python protocol.py 全量一致) ---------- */
  Check(CodeToVk("KeyA") == 0x41, "CodeToVk KeyA");
  Check(CodeToVk("Digit5") == 0x35, "CodeToVk Digit5");
  Check(CodeToVk("F12") == 0x7B, "CodeToVk F12");
  Check(CodeToVk("Enter") == 0x0D, "CodeToVk Enter");
  Check(CodeToVk("Space") == 0x20, "CodeToVk Space");
  Check(CodeToVk("ArrowUp") == 0x26, "CodeToVk ArrowUp");
  Check(CodeToVk("Numpad0") == 0x60, "CodeToVk Numpad0");
  Check(CodeToVk("BogusKey") == 0, "CodeToVk unknown -> 0");
  const auto& fnv_to_code = FnvToCode();
  auto it = fnv_to_code.find(Fnv1a32("KeyA"));
  Check(it != fnv_to_code.end() && it->second == "KeyA", "FnvToCode lookup");

  /* ---------- 5. 事件通道帧(与 Python golden 字节级一致) ---------- */
  std::vector<std::vector<uint8_t>> evs;
  evs.push_back(EvCursorPos(0.75f, -0.125f, 200));
  evs.push_back(EvQuality(4500, 30, 201));
  evs.push_back(EvToast(u8"你好,云桌面", 202));
  CheckBytes("event frame bytes", EncodeEventFrame(evs, 7), golden_events);

  /* ---------- 6. 非法帧必须被拒绝 ---------- */
  {
    uint8_t bad[10] = {'X', 'R', 'I', 'N', 1, 42, 0, 0, 0, 0};
    std::vector<InputEvent> junk;
    uint32_t junk_seq = 0;
    Check(!DecodeInputFrame(bad, sizeof(bad), &junk_seq, &junk), "bad magic rejected");
  }
  {
    uint8_t truncated[21] = {};
    truncated[0] = 'C';
    truncated[1] = 'R';
    truncated[2] = 'I';
    truncated[3] = 'N';
    truncated[4] = 1;
    truncated[9] = 1;   // n = 1
    truncated[10] = 0x02;  // MOVE 事件头,但 body 被截断
    std::vector<InputEvent> junk;
    uint32_t junk_seq = 0;
    Check(!DecodeInputFrame(truncated, sizeof(truncated), &junk_seq, &junk),
          "truncated frame rejected");
  }

  if (g_failures == 0) {
    std::printf("CONTRACT_OK input=%zuB events=%zuB\n", golden_input.size(),
                golden_events.size());
    return 0;
  }
  std::printf("CONTRACT_FAIL %d checks failed\n", g_failures);
  return 1;
}