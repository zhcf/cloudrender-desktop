/**
 * CloudRender 会话核心 C ABI 实现(cloudrender_session.dll)
 *
 * 封装 cr::session::PeerSession + nativecore 直连抓屏/注入,供 Python ctypes
 * (信令壳)等外部语言加载。信令事件不跨线程回调外部代码:内部队列 + 轮询拉取。
 */
#include "cloudrender/capi/cr_session_api.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <new>
#include <string>
#include <utility>
#include <vector>

#include "cloudrender/session/cr_webrtc_session.hpp"
#include "cloudrender/session/nativecore_source.hpp"

namespace {

// ----------------------------------------------------------------------
// 最小 JSON 工具(信令壳传入的 config 只解析固定键)
// ----------------------------------------------------------------------

/// 反转义 JSON 字符串中的常见转义(STUN URL 等场景够用,\u 退化为原字符)。
void JsonUnescape(const std::string& in, std::string* out) {
  out->clear();
  out->reserve(in.size());
  for (size_t i = 0; i < in.size(); ++i) {
    const char c = in[i];
    if (c != '\\' || i + 1 >= in.size()) {
      *out += c;
      continue;
    }
    const char n = in[++i];
    switch (n) {
      case 'b': *out += '\b'; break;
      case 'f': *out += '\f'; break;
      case 'n': *out += '\n'; break;
      case 'r': *out += '\r'; break;
      case 't': *out += '\t'; break;
      default: *out += n; break;  // \" \\ \/ 等
    }
  }
}

/// 读取 "key":<整数>(含负数,允许前后空白);键不存在返回 fallback。
long long JsonGetInt(const std::string& body, const char* key, long long fallback) {
  const std::string k = std::string("\"") + key + "\":";
  const size_t pos = body.find(k);
  if (pos == std::string::npos) return fallback;
  return std::strtoll(body.c_str() + pos + k.size(), nullptr, 10);
}

/// 读取 "key":["a","b"] 字符串数组;键不存在返回 false(保持外层默认值)。
bool JsonGetStringArray(const std::string& body, const char* key,
                        std::vector<std::string>* out) {
  const std::string k = std::string("\"") + key + "\"";
  size_t pos = body.find(k);
  if (pos == std::string::npos) return false;
  pos = body.find('[', pos + k.size());
  if (pos == std::string::npos) return false;
  out->clear();
  size_t i = pos + 1;
  while (i < body.size()) {
    i = body.find_first_of("\"]", i);
    if (i == std::string::npos) return false;  // 未闭合
    if (body[i] == ']') return true;
    ++i;  // 跳过元素开引号
    std::string raw;
    bool closed = false;
    while (i < body.size()) {
      const char c = body[i];
      if (c == '\\' && i + 1 < body.size()) {
        raw += c;
        raw += body[++i];
        ++i;
        continue;
      }
      if (c == '"') {
        closed = true;
        ++i;
        break;
      }
      raw += c;
      ++i;
    }
    if (!closed) return false;
    std::string item;
    JsonUnescape(raw, &item);
    out->push_back(std::move(item));
  }
  return false;  // 未遇到 ']' 前内容耗尽
}

std::string JsonEscape(const std::string& s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += c;
        }
    }
  }
  return out;
}

/// 复制为堆字符串(调用方用 cr_session_free 释放)。
char* DupString(const std::string& s) {
  char* p = static_cast<char*>(std::malloc(s.size() + 1));
  if (p == nullptr) return nullptr;
  std::memcpy(p, s.c_str(), s.size() + 1);
  return p;
}

}  // namespace

// ----------------------------------------------------------------------
// 会话句柄(与头文件 typedef struct cr_session 对应)
// ----------------------------------------------------------------------
struct cr_session {
  cr::source::NativeCoreFrameSource* source = nullptr;
  cr::source::NativeCoreInjector* injector = nullptr;
  cr::session::PeerSession* session = nullptr;

  std::mutex mu;
  std::deque<std::pair<std::string, std::string>> signals;  // (type, payload_json)

  void PushSignal(const std::string& type, const std::string& payload_json) {
    std::lock_guard<std::mutex> lock(mu);
    signals.emplace_back(type, payload_json);
  }
};

// ----------------------------------------------------------------------
extern "C" {

cr_session* CR_SESSION_CALL cr_session_create(const char* config_json) {
  const std::string cfg = config_json != nullptr ? config_json : "";

  const long long monitor = JsonGetInt(cfg, "monitor", -1);
  long long fps = JsonGetInt(cfg, "fps", 30);
  if (fps < 1) fps = 1;
  if (fps > 60) fps = 60;
  const auto window_hwnd =
      static_cast<uint64_t>(JsonGetInt(cfg, "window_hwnd", 0));
  const long long bitrate_kbps = JsonGetInt(cfg, "bitrate_kbps", 0);

  cr_session* w = new (std::nothrow) cr_session();
  if (w == nullptr) return nullptr;

  w->source = new (std::nothrow) cr::source::NativeCoreFrameSource(
      static_cast<int>(monitor), static_cast<int>(fps), window_hwnd);
  w->injector = new (std::nothrow) cr::source::NativeCoreInjector();
  if (w->source == nullptr || w->injector == nullptr) {
    delete w->injector;
    delete w->source;
    delete w;
    return nullptr;
  }

  cr::session::PeerSessionConfig scfg;
  scfg.fps = static_cast<int>(fps);
  // 固定目标码率(kbps→bps,钳制 100..200000;0/缺省=自适应)
  if (bitrate_kbps > 0) {
    const long long clamped = bitrate_kbps < 100
                                  ? 100
                                  : (bitrate_kbps > 200000 ? 200000
                                                           : bitrate_kbps);
    scfg.bitrate_bps = static_cast<int>(clamped * 1000);
  }
  std::vector<std::string> ice_servers;
  if (JsonGetStringArray(cfg, "ice_servers", &ice_servers)) {
    scfg.ice_servers = std::move(ice_servers);
  }

  cr::session::PeerSession::Callbacks cbs;
  cbs.on_signal = [w](const std::string& type, const std::string& payload) {
    if (type == "offer") return;  // offer 由 cr_session_create_offer 同步交付
    w->PushSignal(type, payload);
  };
  cbs.on_closed = [w](const char* reason) {
    w->PushSignal("closed", std::string("{\"reason\":\"") +
                                JsonEscape(reason != nullptr ? reason : "") +
                                "\"}");
  };

  w->session = new (std::nothrow)
      cr::session::PeerSession(w->source, w->injector, scfg, std::move(cbs));
  if (w->session == nullptr || !w->session->valid()) {
    delete w->session;  // 先停会话(线程/观察者),再释放源与句柄
    delete w->injector;
    delete w->source;
    delete w;
    return nullptr;
  }
  return w;
}

void CR_SESSION_CALL cr_session_destroy(cr_session* s) {
  if (s == nullptr) return;
  if (s->session != nullptr) {
    s->session->Close();  // 幂等;停止帧泵/取消观察者后再释放
    delete s->session;
  }
  delete s->injector;
  delete s->source;
  delete s;
}

char* CR_SESSION_CALL cr_session_create_offer(cr_session* s) {
  if (s == nullptr || s->session == nullptr) return nullptr;
  std::string sdp;
  if (!s->session->CreateOffer(&sdp) || sdp.empty()) return nullptr;
  s->session->StartStreaming();  // 帧泵:连接建立后推送(重复调用幂等)
  return DupString(sdp);
}

int CR_SESSION_CALL cr_session_handle_signal(cr_session* s, const char* type,
                                             const char* payload_json) {
  if (s == nullptr || s->session == nullptr) return CR_SESSION_E_STATE;
  if (type == nullptr) return CR_SESSION_E_ARG;
  s->session->HandleSignal(type, payload_json != nullptr ? payload_json : "");
  return CR_SESSION_OK;
}

char* CR_SESSION_CALL cr_session_poll_signal(cr_session* s) {
  if (s == nullptr) return nullptr;
  std::pair<std::string, std::string> item;
  {
    std::lock_guard<std::mutex> lock(s->mu);
    if (s->signals.empty()) return nullptr;
    item = std::move(s->signals.front());
    s->signals.pop_front();
  }
  const std::string out = "{\"type\":\"" + JsonEscape(item.first) +
                          "\",\"payload\":" +
                          (item.second.empty() ? "null" : item.second) + "}";
  return DupString(out);
}

int CR_SESSION_CALL cr_session_connected(cr_session* s) {
  return (s != nullptr && s->session != nullptr && s->session->connected()) ? 1
                                                                            : 0;
}

int CR_SESSION_CALL cr_session_get_source_size(cr_session* s, int* width,
                                               int* height) {
  if (s == nullptr || width == nullptr || height == nullptr) {
    return CR_SESSION_E_ARG;
  }
  if (s->source == nullptr) return CR_SESSION_E_STATE;
  const int w = s->source->width();
  const int h = s->source->height();
  if (w <= 0 || h <= 0) return CR_SESSION_E_STATE;
  *width = w;
  *height = h;
  return CR_SESSION_OK;
}

char* CR_SESSION_CALL cr_session_get_stats(cr_session* s) {
  if (s == nullptr || s->session == nullptr) return nullptr;
  uint64_t bytes_sent = 0;
  uint64_t frames_pushed = 0;
  if (!s->session->QueryStats(&bytes_sent, &frames_pushed)) return nullptr;
  char buf[128];
  std::snprintf(buf, sizeof(buf), "{\"bytes_sent\":%llu,\"frames_pushed\":%llu}",
                static_cast<unsigned long long>(bytes_sent),
                static_cast<unsigned long long>(frames_pushed));
  return DupString(buf);
}

int CR_SESSION_CALL cr_session_set_bitrate(cr_session* s, int bitrate_bps) {
  if (s == nullptr || s->session == nullptr) return CR_SESSION_E_STATE;
  if (bitrate_bps <= 0) return CR_SESSION_E_ARG;
  return s->session->SetBitrate(bitrate_bps) ? CR_SESSION_OK
                                             : CR_SESSION_E_FAIL;
}

int CR_SESSION_CALL cr_session_set_secure_mode(cr_session* s, int on) {
  if (s == nullptr || s->session == nullptr) return CR_SESSION_E_STATE;
  s->session->SetSecureMode(on != 0);
  return CR_SESSION_OK;
}

int CR_SESSION_CALL cr_session_push_secure_frame(cr_session* s,
                                                 const uint8_t* bgra, int width,
                                                 int height, int stride,
                                                 uint64_t pts_us) {
  if (s == nullptr || s->session == nullptr) return CR_SESSION_E_STATE;
  if (bgra == nullptr || width <= 0 || height <= 0) return CR_SESSION_E_ARG;
  if (stride < 0 || (stride > 0 && stride < width * 4)) {
    return CR_SESSION_E_ARG;
  }
  s->session->PushSecureFrame(bgra, width, height, stride, pts_us);
  return CR_SESSION_OK;
}

int CR_SESSION_CALL cr_session_send_toast(cr_session* s,
                                          const char* utf8_text) {
  if (s == nullptr || s->session == nullptr) return CR_SESSION_E_STATE;
  if (utf8_text == nullptr) return CR_SESSION_E_ARG;
  return s->session->SendToast(utf8_text) ? CR_SESSION_OK : CR_SESSION_E_FAIL;
}

void CR_SESSION_CALL cr_session_close(cr_session* s) {
  if (s != nullptr && s->session != nullptr) s->session->Close();
}

void CR_SESSION_CALL cr_session_free(char* p) { std::free(p); }

}  // extern "C"