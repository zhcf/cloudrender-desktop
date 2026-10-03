/**
 * CloudRender WebRTC 会话层(C++)——webrtc-sdk libwebrtc facade 实现
 *
 * 以 webrtc-sdk 预编译包(rtc_*.h facade 封装,非原生头文件)实现服务端 offer 会话:
 *  - CreateOffer:RTCPeerConnectionFactory + 自定义帧源 + input/events DataChannel + offer
 *  - HandleSignal:answer / ice / disconnect(payload 为 payload 对象的 JSON)
 *  - StartStreaming:帧泵线程(取最新帧 → BGRA→I420 → RTCVideoSource::OnCapturedFrame)
 *  - 输入通道二进制帧经 cr::wire 解码后直接回调注入器(TOUCH 按 noVNC
 *    惯例映射鼠标/滚轮);锁屏(安全桌面)模式下列为外部帧注入 + 输入旁路转发
 *
 * 线程模型:facade 内建 worker/network/signaling 线程,观察者回调由内部线程触发;
 * CreateOffer 用 condition_variable 同步等待内置回调(带超时),方法可从任意线程调用。
 *
 * v1 已知限制(与 Python 侧对齐待后续):
 *  - facade 无 GenerateKeyFrame,video_lost 关键帧请求暂不可做
 */
#include "cloudrender/session/cr_webrtc_session.hpp"

#if defined(CR_HAVE_WEBRTC)

// timeBeginPeriod/timeEndPeriod:提升进程定时器分辨率(帧泵高精度节拍用;
// NOMINMAX 已在构建定义,避免 min/max 宏污染)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <timeapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "libwebrtc.h"
#include "rtc_data_channel.h"
#include "rtc_ice_candidate.h"
#include "rtc_media_stream.h"
#include "rtc_mediaconstraints.h"
#include "rtc_peerconnection.h"
#include "rtc_peerconnection_factory.h"
#include "rtc_rtp_sender.h"
#include "rtc_video_frame.h"
#include "rtc_video_source.h"
#include "rtc_video_track.h"

#include "cloudrender/wire/cr_wire.hpp"

namespace cr::session {
namespace {

// ----------------------------------------------------------------------
// 全局初始化:libwebrtc 线程/SSL 为进程级资源,用引用计数管理生命周期
// ----------------------------------------------------------------------
std::mutex g_lib_mu;
int g_lib_ref = 0;

bool AcquireLibWebRTC() {
  std::lock_guard<std::mutex> lock(g_lib_mu);
  if (g_lib_ref == 0 && !libwebrtc::LibWebRTC::Initialize()) return false;
  ++g_lib_ref;
  return true;
}

void ReleaseLibWebRTC() {
  std::lock_guard<std::mutex> lock(g_lib_mu);
  if (g_lib_ref > 0 && --g_lib_ref == 0) libwebrtc::LibWebRTC::Terminate();
}

// ----------------------------------------------------------------------
// 进程定时器分辨率:Windows 默认 15.625ms 粒度会把帧泵 sleep 撑大(30fps
// 实测只有 ~29fps),启停帧泵时经引用计数提升到 1ms(进程级资源)。
// ----------------------------------------------------------------------
std::atomic<int> g_timer_ref{0};

void AcquireTimerResolution() {
  if (g_timer_ref.fetch_add(1) == 0) ::timeBeginPeriod(1);
}

void ReleaseTimerResolution() {
  if (g_timer_ref.fetch_sub(1) == 1) ::timeEndPeriod(1);
}

// ----------------------------------------------------------------------
// 最小 JSON 工具(信令 payload 由上层透传,只提取固定键)
// ----------------------------------------------------------------------
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
      case '"': *out += '"'; break;
      case '\\': *out += '\\'; break;
      case '/': *out += '/'; break;
      case 'b': *out += '\b'; break;
      case 'f': *out += '\f'; break;
      case 'n': *out += '\n'; break;
      case 'r': *out += '\r'; break;
      case 't': *out += '\t'; break;
      case 'u': {
        // \uXXXX:BMP 内码点转 UTF-8(SDP/候选串里几乎不出现,做容错处理)
        if (i + 4 < in.size()) {
          unsigned code = 0;
          bool valid = true;
          for (int k = 1; k <= 4; ++k) {
            const char h = in[i + k];
            unsigned v = 0;
            if (h >= '0' && h <= '9') v = static_cast<unsigned>(h - '0');
            else if (h >= 'a' && h <= 'f') v = static_cast<unsigned>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') v = static_cast<unsigned>(h - 'A' + 10);
            else { valid = false; break; }
            code = code * 16 + v;
          }
          if (valid) {
            i += 4;
            if (code < 0x80) {
              *out += static_cast<char>(code);
            } else if (code < 0x800) {
              *out += static_cast<char>(0xC0 | (code >> 6));
              *out += static_cast<char>(0x80 | (code & 0x3F));
            } else {
              *out += static_cast<char>(0xE0 | (code >> 12));
              *out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
              *out += static_cast<char>(0x80 | (code & 0x3F));
            }
            break;
          }
        }
        *out += n;
        break;
      }
      default:
        *out += n;
        break;
    }
  }
}

/// 提取 "key":"value" 的值(按 JSON 转义规则反转义);找不到/未闭合返回 false。
/// 容忍冒号后空白(Python json.dumps 默认输出 ": ")与键的多次出现
/// (如 "candidate" 外层为对象、内层才是字符串):跳过值非字符串的匹配。
bool JsonExtractString(const std::string& body, const char* key, std::string* out) {
  const std::string k = std::string("\"") + key + "\"";
  size_t search_from = 0;
  while (true) {
    const size_t pos = body.find(k, search_from);
    if (pos == std::string::npos) return false;
    search_from = pos + k.size();
    // 键后(允许空白)必须是冒号
    const size_t colon = body.find(':', search_from);
    if (colon == std::string::npos) return false;
    bool gap_ok = true;
    for (size_t i = search_from; i < colon; ++i) {
      const char c = body[i];
      if (c != ' ' && c != '\t' && c != '\r' && c != '\n') { gap_ok = false; break; }
    }
    if (!gap_ok) continue;  // 该次出现不是键(如处于字符串值内部)
    size_t v = colon + 1;
    while (v < body.size() && (body[v] == ' ' || body[v] == '\t' ||
                               body[v] == '\r' || body[v] == '\n')) ++v;
    if (v >= body.size() || body[v] != '"') continue;  // 值非字符串:找下一次出现
    ++v;
    std::string raw;
    bool closed = false;
    for (size_t i = v; i < body.size(); ++i) {
      const char c = body[i];
      if (c == '\\' && i + 1 < body.size()) {
        raw += c;
        raw += body[++i];
        continue;
      }
      if (c == '"') { closed = true; break; }
      raw += c;
    }
    if (!closed) return false;
    JsonUnescape(raw, out);
    return true;
  }
}

/// 提取 "key":123(整数);找不到返回 fallback。
int JsonExtractInt(const std::string& body, const char* key, int fallback) {
  const std::string k = std::string("\"") + key + "\":";
  const size_t pos = body.find(k);
  if (pos == std::string::npos) return fallback;
  return std::atoi(body.c_str() + pos + k.size());
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

std::string IcePayloadJson(const std::string& candidate, const std::string& sdp_mid,
                           int mline_index) {
  return "{\"candidate\":{\"candidate\":\"" + JsonEscape(candidate) +
         "\",\"sdpMid\":\"" + JsonEscape(sdp_mid) + "\",\"sdpMLineIndex\":" +
         std::to_string(mline_index) + "}}";
}

std::string SdpPayloadJson(const std::string& sdp) {
  return "{\"sdp\":\"" + JsonEscape(sdp) + "\"}";
}

/// 字节流 → 小写 hex(锁屏期间输入帧经信令旁路转发用)。
std::string HexEncode(const char* data, int length) {
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(static_cast<size_t>(length) * 2);
  for (int i = 0; i < length; ++i) {
    const unsigned char c = static_cast<unsigned char>(data[i]);
    out += kHex[c >> 4];
    out += kHex[c & 0x0F];
  }
  return out;
}

/// 当前 Unix 毫秒(事件帧时间戳;对齐 Python time.time()*1000)。
uint64_t NowMs() {
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count());
}

// ----------------------------------------------------------------------
// SDP 码率提示:带宽估计(GCC)默认从 ~300kbps 慢爬,启动前十几秒画面糊;
// libwebrtc 视频引擎认 x-google-*-bitrate(fmtp 参数,kbps)作为发送侧
// codec 的起/上/下限,让编码器起步即按目标码率工作。注入 answer 的每个
// 视频 codec(跳过 rtx 的 apt= 行):已有 fmtp 行追加,无则 rtpmap 后插行。
// ----------------------------------------------------------------------
std::string InjectVideoBitrateHints(const std::string& sdp, int bitrate_kbps) {
  if (bitrate_kbps <= 0) return sdp;
  const std::string kbps = std::to_string(bitrate_kbps);
  const std::string extra = "x-google-start-bitrate=" + kbps +
                            ";x-google-min-bitrate=" + kbps +
                            ";x-google-max-bitrate=" + kbps;

  std::vector<std::string> lines;  // 按 '\n' 切行(行尾保留 '\r')
  size_t begin = 0;
  for (;;) {
    const size_t pos = sdp.find('\n', begin);
    if (pos == std::string::npos) {
      // 仅剩非空才入列:SDP 以换行结尾时不再产生空尾行(解析器不容忍)
      if (begin < sdp.size()) lines.push_back(sdp.substr(begin));
      break;
    }
    lines.push_back(sdp.substr(begin, pos - begin));
    begin = pos + 1;
  }

  size_t video_begin = lines.size();  // m=video 段范围
  size_t video_end = lines.size();
  for (size_t i = 0; i < lines.size(); ++i) {
    if (lines[i].compare(0, 7, "m=video") == 0) {
      video_begin = i;
    } else if (video_begin < lines.size() && lines[i].compare(0, 2, "m=") == 0) {
      video_end = i;
      break;
    }
  }
  if (video_begin == lines.size()) return sdp;  // 无视频段,原样返回

  std::vector<std::string> rtx_pts;   // 带 apt= 的 rtx pt
  std::vector<std::string> fmtp_pts;  // 已有 fmtp 的 pt
  for (size_t i = video_begin + 1; i < video_end; ++i) {
    const std::string& ln = lines[i];
    if (ln.compare(0, 9, "a=rtpmap:") == 0) {
      continue;  // rtpmap 信息在输出阶段按行就地解析
    } else if (ln.compare(0, 7, "a=fmtp:") == 0) {
      const size_t sp = ln.find(' ', 7);
      if (sp == std::string::npos) continue;
      const std::string pt = ln.substr(7, sp - 7);
      if (ln.find("apt=", sp) != std::string::npos)
        rtx_pts.push_back(pt);
      else
        fmtp_pts.push_back(pt);
    }
  }
  auto has = [](const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
  };

  std::string out;
  for (size_t i = 0; i < lines.size(); ++i) {
    std::string ln = lines[i];
    const bool in_video = i > video_begin && i < video_end;
    const bool cr = !ln.empty() && ln.back() == '\r';
    if (cr) ln.pop_back();
    std::string pt;
    if (in_video && ln.compare(0, 9, "a=rtpmap:") == 0) {
      const size_t sp = ln.find(' ', 9);
      if (sp != std::string::npos) pt = ln.substr(9, sp - 9);
    } else if (in_video && ln.compare(0, 7, "a=fmtp:") == 0) {
      const size_t sp = ln.find(' ', 7);
      if (sp != std::string::npos && !has(rtx_pts, ln.substr(7, sp - 7)))
        ln += ";" + extra;  // 已有 fmtp:行尾追加(rtx 除外)
    }
    out += ln;
    out += cr ? "\r\n" : "\n";
    if (!pt.empty() && !has(fmtp_pts, pt) && !has(rtx_pts, pt)) {
      out += "a=fmtp:" + pt + " " + extra;  // 无 fmtp:rtpmap 后插一行
      out += cr ? "\r\n" : "\n";
    }
  }
  return out;
}

// ----------------------------------------------------------------------
// BGRA32 → I420(BT.601,U/V 用 2×2 均值采样)
// ----------------------------------------------------------------------
void BgraToI420(const uint8_t* bgra, int stride, int width, int height,
                uint8_t* dst_y, int stride_y, uint8_t* dst_u, int stride_u,
                uint8_t* dst_v, int stride_v) {
  for (int y = 0; y < height; ++y) {
    const uint8_t* row = bgra + static_cast<size_t>(y) * stride;
    uint8_t* yrow = dst_y + static_cast<size_t>(y) * stride_y;
    for (int x = 0; x < width; ++x) {
      int b = row[x * 4 + 0], g = row[x * 4 + 1], r = row[x * 4 + 2];
      yrow[x] = static_cast<uint8_t>(((66 * r + 129 * g + 25 * b + 128) >> 8) + 16);
    }
  }
  for (int y = 0; y < height / 2; ++y) {
    const uint8_t* r0 = bgra + static_cast<size_t>(2 * y) * stride;
    const uint8_t* r1 = r0 + stride;
    uint8_t* urow = dst_u + static_cast<size_t>(y) * stride_u;
    uint8_t* vrow = dst_v + static_cast<size_t>(y) * stride_v;
    for (int x = 0; x < width / 2; ++x) {
      int b = 0, g = 0, r = 0;
      for (int dy = 0; dy < 2; ++dy) {
        const uint8_t* row = dy ? r1 : r0;
        for (int dx = 0; dx < 2; ++dx) {
          const uint8_t* p = row + static_cast<size_t>(2 * x + dx) * 4;
          b += p[0];
          g += p[1];
          r += p[2];
        }
      }
      b /= 4; g /= 4; r /= 4;
      urow[x] = static_cast<uint8_t>(((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128);
      vrow[x] = static_cast<uint8_t>(((112 * r - 94 * g - 18 * b + 128) >> 8) + 128);
    }
  }
}

constexpr auto kOfferTimeout = std::chrono::seconds(5);

}  // namespace

// ----------------------------------------------------------------------
// 会话内部实现(内含 libwebrtc 观察者)
// ----------------------------------------------------------------------
struct PeerSession::Impl final : public libwebrtc::RTCPeerConnectionObserver,
                                 public libwebrtc::RTCDataChannelObserver {
  cr::source::FrameSource* source;
  cr::source::InputInjector* injector;
  PeerSessionConfig config;
  Callbacks cbs;

  bool lib_inited_ = false;
  bool init_ok_ = false;
  libwebrtc::scoped_refptr<libwebrtc::RTCPeerConnectionFactory> factory_;
  libwebrtc::scoped_refptr<libwebrtc::RTCPeerConnection> pc_;
  libwebrtc::scoped_refptr<libwebrtc::RTCVideoSource> video_source_;
  libwebrtc::scoped_refptr<libwebrtc::RTCVideoTrack> video_track_;
  libwebrtc::scoped_refptr<libwebrtc::RTCRtpSender> rtp_sender_;
  libwebrtc::scoped_refptr<libwebrtc::RTCDataChannel> dc_input_;
  libwebrtc::scoped_refptr<libwebrtc::RTCDataChannel> dc_events_;
  libwebrtc::scoped_refptr<libwebrtc::RTCMediaConstraints> offer_constraints_;

  // 安全桌面(锁屏)模式:帧泵改推 PushSecureFrame 注入的帧(普通权限进程
  // 锁屏时无法抓屏);输入帧经 on_signal("secure_input") 旁路转发上层。
  std::atomic<bool> secure_mode_{false};
  std::mutex secure_mu_;
  cr::source::Frame secure_frame_;  // secure_mu_ 保护
  bool has_secure_frame_ = false;

  std::atomic<uint32_t> events_seq_{0};  // events 通道(toast)帧序号

  // 触摸手势状态机(noVNC 惯例,移植 Python _apply_touch_sync;
  // 仅输入分发线程(DataChannel 回调)访问,无需加锁)
  std::map<uint8_t, std::pair<float, float>> touches_;
  bool touch_left_down_ = false;
  bool touch_right_down_ = false;
  bool touch_scroll_ = false;
  std::optional<std::pair<float, float>> touch_scroll_mid_;

  std::atomic<bool> connected_{false};
  std::atomic<bool> closed_{false};
  std::atomic<bool> timer_acquired_{false};  // timeBeginPeriod 已调用(须配对释放)
  int applied_bitrate_bps_ = -1;  // 已应用的目标码率(仅变化时打日志)
  int64_t stats_target_bps_ = -1;  // 编码器目标码率(诊断;仅变化时打)
  std::string stats_qlr_;          // 质量受限原因(诊断;随目标一起打)

  std::mutex offer_mu_;
  std::condition_variable offer_cv_;
  bool offer_pending_ = false;
  std::string offer_sdp_;

  std::thread sender_;

  // 发送侧统计:帧泵累计帧数(差分算 fps) + GetStats 同步等待(模式同 offer)
  std::atomic<uint64_t> frames_pushed_{0};
  std::mutex stats_mu_;
  std::condition_variable stats_cv_;
  bool stats_pending_ = false;
  bool stats_ok_ = false;
  uint64_t stats_bytes_sent_ = 0;

  Impl(cr::source::FrameSource* src, cr::source::InputInjector* inj,
       const PeerSessionConfig& cfg, Callbacks cb)
      : source(src), injector(inj), config(cfg), cbs(std::move(cb)) {}

  ~Impl() {
    Close();
    if (lib_inited_) ReleaseLibWebRTC();
  }

  // ---------------- 工厂与 PeerConnection 初始化 ----------------
  bool Init() {
    lib_inited_ = AcquireLibWebRTC();
    if (!lib_inited_) return false;
    factory_ = libwebrtc::LibWebRTC::CreateRTCPeerConnectionFactory();
    if (factory_ == nullptr) return false;
    // facade 两阶段初始化:此处才真正创建原生工厂与 worker/signaling/network 线程,
    // 缺此调用则后续 Create/CreateCustomVideoSource 崩溃(已用分步探针验证)。
    if (!factory_->Initialize()) return false;

    libwebrtc::RTCConfiguration rtc_config;
    int i = 0;
    for (const auto& url : config.ice_servers) {
      if (i >= libwebrtc::kMaxIceServerSize) break;
      rtc_config.ice_servers[i++].uri = url;
    }
    rtc_config.offer_to_receive_video = false;  // 单向视频:服务端只发不收
    rtc_config.offer_to_receive_audio = false;
    rtc_config.sdp_semantics = libwebrtc::SdpSemantics::kUnifiedPlan;
    // 固定码率:screencast_min_bitrate(kbps)给视频流抬底——单流场景
    // encodings.min_bitrate_bps 会被 native 丢弃(仅多流层分配用),此字段
    // 是引擎级最小码率入口,GCC 目标起步即按目标值分配;local_video_
    // bandwidth 同源(默认仅 512kbps,与实测起步值吻合)。
    if (config.bitrate_bps > 0) {
      rtc_config.screencast_min_bitrate = config.bitrate_bps / 1000;
      rtc_config.local_video_bandwidth =
          static_cast<uint32_t>(config.bitrate_bps / 1000);
    }

    pc_ = factory_->Create(rtc_config, libwebrtc::RTCMediaConstraints::Create());
    if (pc_ == nullptr) return false;
    pc_->RegisterRTCPeerConnectionObserver(this);

    // 自定义帧源:桌面帧经 nativecore 适配器提供,此处只负责注入
    video_source_ = factory_->CreateCustomVideoSource(
        "cloudrender_screen", libwebrtc::RTCMediaConstraints::Create());
    if (video_source_ == nullptr) return false;
    video_track_ = factory_->CreateVideoTrack(video_source_, "cloudrender_screen");
    if (video_track_ == nullptr) return false;

    {
      std::vector<std::string> stream_ids_std{"cloudrender"};
      libwebrtc::vector<libwebrtc::string> stream_ids(stream_ids_std);
      rtp_sender_ = pc_->AddTrack(video_track_, stream_ids);
    }
    if (rtp_sender_ == nullptr) return false;

    // 服务端创建两条数据通道:input(C→S 输入)、events(S→C 事件)
    libwebrtc::RTCDataChannelInit dc_init;  // ordered 默认 true
    dc_input_ = pc_->CreateDataChannel("input", &dc_init);
    if (dc_input_ != nullptr) dc_input_->RegisterObserver(this);
    dc_events_ = pc_->CreateDataChannel("events", &dc_init);

    // offer 约束:C→S 不收视频/音频(与 RTCConfiguration 双保险)
    offer_constraints_ = libwebrtc::RTCMediaConstraints::Create();
    if (offer_constraints_ != nullptr) {
      offer_constraints_->AddOptionalConstraint(
          libwebrtc::RTCMediaConstraints::kOfferToReceiveVideo,
          libwebrtc::RTCMediaConstraints::kValueFalse);
      offer_constraints_->AddOptionalConstraint(
          libwebrtc::RTCMediaConstraints::kOfferToReceiveAudio,
          libwebrtc::RTCMediaConstraints::kValueFalse);
    }

    if (source != nullptr && !source->Start()) return false;  // 抓屏启动失败=会话不可用
    init_ok_ = true;
    return true;
  }

  // ---------------- 目标码率 ----------------
  /// 设 min=max=指定值:带宽估计起步偏低会让前十几秒画面又糊又卡;固定后
  /// 起步即按目标码率编码(对齐 aiortc 版 target_bitrate 固定模式)。幂等,
  /// 运行期可由上层周期重申。仅值变化时打一条日志(避免周期重申刷屏)。
  bool SetBitrate(int bitrate_bps) {
    if (bitrate_bps <= 0 || rtp_sender_ == nullptr || closed_.load())
      return false;
    libwebrtc::scoped_refptr<libwebrtc::RTCRtpParameters> params =
        rtp_sender_->parameters();
    if (params == nullptr) return false;
    const auto encodings = params->encodings();
    if (encodings.size() == 0) return false;
    for (size_t i = 0; i < encodings.size(); ++i) {
      encodings[i]->set_min_bitrate_bps(bitrate_bps);
      encodings[i]->set_max_bitrate_bps(bitrate_bps);
    }
    const bool ok = rtp_sender_->set_parameters(params);
    if (bitrate_bps != applied_bitrate_bps_) {
      std::fprintf(stderr, "[cloudrender] 目标码率 %d bps:apply %s\n",
                   bitrate_bps, ok ? "ok" : "FAILED");
    }
    applied_bitrate_bps_ = bitrate_bps;
    return ok;
  }

  // ---------------- offer(同步等待内置回调) ----------------
  bool CreateOffer(std::string* sdp_out) {
    if (pc_ == nullptr || closed_.load()) return false;
    {
      std::lock_guard<std::mutex> lock(offer_mu_);
      offer_pending_ = true;
      offer_sdp_.clear();
    }

    pc_->CreateOffer(
        libwebrtc::OnSdpCreateSuccess(
            [this](const libwebrtc::string sdp, const libwebrtc::string type) {
              {
                std::lock_guard<std::mutex> lock(offer_mu_);
                offer_sdp_ = sdp.std_string();
              }
              if (pc_ == nullptr) return;
              pc_->SetLocalDescription(
                  sdp, type,
                  libwebrtc::OnSetSdpSuccess([this]() {
                    std::string sdp_copy;
                    {
                      std::lock_guard<std::mutex> lock(offer_mu_);
                      sdp_copy = offer_sdp_;
                    }
                    if (cbs.on_signal) cbs.on_signal("offer", SdpPayloadJson(sdp_copy));
                    {
                      std::lock_guard<std::mutex> lock(offer_mu_);
                      offer_pending_ = false;
                    }
                    offer_cv_.notify_all();
                  }),
                  libwebrtc::OnSetSdpFailure([this](const char* error) {
                    {
                      std::lock_guard<std::mutex> lock(offer_mu_);
                      offer_pending_ = false;
                    }
                    offer_cv_.notify_all();
                    if (cbs.on_closed)
                      cbs.on_closed(error ? error : "set local description failed");
                  }));
            }),
        libwebrtc::OnSdpCreateFailure([this](const char* error) {
          {
            std::lock_guard<std::mutex> lock(offer_mu_);
            offer_pending_ = false;
          }
          offer_cv_.notify_all();
          if (cbs.on_closed) cbs.on_closed(error ? error : "create offer failed");
        }),
        offer_constraints_);

    std::unique_lock<std::mutex> lock(offer_mu_);
    const bool done = offer_cv_.wait_for(lock, kOfferTimeout,
                                         [this] { return !offer_pending_; });
    if (!done || offer_sdp_.empty()) return false;
    *sdp_out = offer_sdp_;
    SetBitrate(config.bitrate_bps);  // 固定码率:先设一次;运行期由上层周期重申
    return true;
  }

  // ---------------- 统计(outbound-rtp bytesSent + 帧泵计数) ----------------
  bool QueryStats(uint64_t* bytes_sent, uint64_t* frames_pushed) {
    if (pc_ == nullptr || closed_.load() || rtp_sender_ == nullptr) return false;
    {
      std::lock_guard<std::mutex> lock(stats_mu_);
      stats_pending_ = true;
      stats_ok_ = false;
    }
    pc_->GetStats(
        rtp_sender_,
        libwebrtc::OnStatsCollectorSuccess(
            [this](const libwebrtc::vector<
                   libwebrtc::scoped_refptr<libwebrtc::MediaRTCStats>> reports) {
              uint64_t bytes = 0;
              int64_t target_bps = -1;  // 诊断:编码器目标码率(bps)
              std::string qlr;          // 诊断:质量受限原因
              for (size_t i = 0; i < reports.size(); ++i) {
                const auto& report = reports[i];
                if (report == nullptr) continue;
                if (report->type().std_string() != "outbound-rtp") continue;
                const auto& members = report->Members();
                for (size_t j = 0; j < members.size(); ++j) {
                  const auto& member = members[j];
                  if (member == nullptr || !member->IsDefined()) continue;
                  const std::string name = member->GetName().std_string();
                  if (name == "bytesSent") {
                    switch (member->GetType()) {
                      case libwebrtc::RTCStatsMember::kUint64:
                        bytes = member->ValueUint64();
                        break;
                      case libwebrtc::RTCStatsMember::kInt64:
                        bytes = static_cast<uint64_t>(member->ValueInt64());
                        break;
                      case libwebrtc::RTCStatsMember::kUint32:
                        bytes = member->ValueUint32();
                        break;
                      default:
                        break;
                    }
                  } else if (name == "targetBitrate") {
                    switch (member->GetType()) {
                      case libwebrtc::RTCStatsMember::kUint32:
                        target_bps = member->ValueUint32();
                        break;
                      case libwebrtc::RTCStatsMember::kInt32:
                        target_bps = member->ValueInt32();
                        break;
                      case libwebrtc::RTCStatsMember::kInt64:
                        target_bps = member->ValueInt64();
                        break;
                      case libwebrtc::RTCStatsMember::kUint64:
                        target_bps =
                            static_cast<int64_t>(member->ValueUint64());
                        break;
                      case libwebrtc::RTCStatsMember::kDouble:
                        target_bps =
                            static_cast<int64_t>(member->ValueDouble());
                        break;
                      default:
                        break;
                    }
                  } else if (name == "qualityLimitationReason") {
                    if (member->GetType() ==
                        libwebrtc::RTCStatsMember::kString)
                      qlr = member->ValueString().std_string();
                  }
                }
              }
              // 诊断:编码器目标(100kbps 粒度)/受限原因变化时打一条(观测带宽
              // 估计抬底效果;纯 ASCII 避免控制台代码页乱码;阈值滤 VBR 抖动)
              const bool bitrate_changed =
                  target_bps >= 0 &&
                  (stats_target_bps_ < 0 ||
                   target_bps / 100000 != stats_target_bps_ / 100000);
              if (bitrate_changed || qlr != stats_qlr_) {
                std::fprintf(stderr,
                             "[cloudrender] enc target %lld bps qlr:%s\n",
                             static_cast<long long>(target_bps),
                             qlr.empty() ? "-" : qlr.c_str());
                stats_target_bps_ = target_bps;
                stats_qlr_ = qlr;
              }
              {
                std::lock_guard<std::mutex> lock(stats_mu_);
                stats_bytes_sent_ = bytes;
                stats_ok_ = true;
                stats_pending_ = false;
              }
              stats_cv_.notify_all();
            }),
        libwebrtc::OnStatsCollectorFailure([this](const char* /*error*/) {
          {
            std::lock_guard<std::mutex> lock(stats_mu_);
            stats_pending_ = false;
          }
          stats_cv_.notify_all();
        }));

    std::unique_lock<std::mutex> lock(stats_mu_);
    const bool done = stats_cv_.wait_for(lock, kOfferTimeout,
                                         [this] { return !stats_pending_; });
    if (!done || !stats_ok_) return false;
    if (bytes_sent != nullptr) *bytes_sent = stats_bytes_sent_;
    if (frames_pushed != nullptr) *frames_pushed = frames_pushed_.load();
    return true;
  }

  // ---------------- 信令处理 ----------------
  void HandleSignal(const std::string& type, const std::string& payload_json) {
    if (type == "answer") {
      if (pc_ == nullptr || closed_.load()) return;
      std::string sdp;
      if (!JsonExtractString(payload_json, "sdp", &sdp)) {
        sdp = payload_json;  // 容错:上游可能直传纯 SDP 文本
      }
      if (sdp.empty()) return;
      // 码率提示:带宽估计起步低会让前十几秒画面糊(x-google-*-bitrate)
      sdp = InjectVideoBitrateHints(sdp, config.bitrate_bps / 1000);
      pc_->SetRemoteDescription(
          sdp, "answer", libwebrtc::OnSetSdpSuccess([]() {}),
          libwebrtc::OnSetSdpFailure([this](const char* error) {
            if (closed_.load()) return;
            if (cbs.on_closed)
              cbs.on_closed(error ? error : "set remote description failed");
          }));
    } else if (type == "ice") {
      if (pc_ == nullptr || closed_.load()) return;
      // payload 形如 {"candidate":{"candidate":"candidate:...","sdpMid":"0",
      // "sdpMLineIndex":0}};也兼容顶层平铺字段(全文档搜索固定键)
      std::string candidate, sdp_mid;
      if (!JsonExtractString(payload_json, "candidate", &candidate) ||
          candidate.empty())
        return;
      JsonExtractString(payload_json, "sdpMid", &sdp_mid);
      const int mline = JsonExtractInt(payload_json, "sdpMLineIndex", 0);
      pc_->AddCandidate(sdp_mid, mline, candidate);  // 候选串保持原样(含前缀)
    } else if (type == "disconnect") {
      Close();
    }
  }

  // ---------------- RTCPeerConnectionObserver ----------------
  void OnSignalingState(libwebrtc::RTCSignalingState /*state*/) override {}

  void OnPeerConnectionState(libwebrtc::RTCPeerConnectionState state) override {
    if (closed_.load()) return;
    connected_ = state == libwebrtc::RTCPeerConnectionStateConnected;
    if (state == libwebrtc::RTCPeerConnectionStateFailed ||
        state == libwebrtc::RTCPeerConnectionStateClosed) {
      if (cbs.on_closed) cbs.on_closed("peer connection state changed");
    }
  }

  void OnIceGatheringState(libwebrtc::RTCIceGatheringState /*state*/) override {}

  void OnIceConnectionState(libwebrtc::RTCIceConnectionState state) override {
    if (closed_.load()) return;
    if (state == libwebrtc::RTCIceConnectionStateConnected ||
        state == libwebrtc::RTCIceConnectionStateCompleted) {
      connected_ = true;
    }
  }

  void OnIceCandidate(libwebrtc::scoped_refptr<libwebrtc::RTCIceCandidate> candidate) override {
    if (closed_.load() || candidate.get() == nullptr) return;
    libwebrtc::string sdp;
    if (!candidate->ToString(sdp) || sdp.size() == 0) return;
    if (cbs.on_signal) {
      cbs.on_signal("ice", IcePayloadJson(sdp.std_string(),
                                          candidate->sdp_mid().std_string(),
                                          candidate->sdp_mline_index()));
    }
  }

  void OnAddStream(libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream> /*stream*/) override {}

  void OnRemoveStream(libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream> /*stream*/) override {}

  void OnDataChannel(libwebrtc::scoped_refptr<libwebrtc::RTCDataChannel> channel) override {
    if (channel.get() == nullptr) return;
    const std::string label = channel->label().std_string();
    if (label == "input") {
      dc_input_ = channel;
      channel->RegisterObserver(this);
    } else if (label == "events") {
      dc_events_ = channel;
    }
  }

  void OnRenegotiationNeeded() override {}

  void OnTrack(libwebrtc::scoped_refptr<libwebrtc::RTCRtpTransceiver> /*transceiver*/) override {}

  void OnAddTrack(libwebrtc::vector<libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream>> /*streams*/,
                  libwebrtc::scoped_refptr<libwebrtc::RTCRtpReceiver> /*receiver*/) override {}

  void OnRemoveTrack(libwebrtc::scoped_refptr<libwebrtc::RTCRtpReceiver> /*receiver*/) override {}

  // ---------------- RTCDataChannelObserver ----------------
  void OnStateChange(libwebrtc::RTCDataChannelState /*state*/) override {}

  void OnMessage(const char* buffer, int length, bool /*binary*/) override {
    if (closed_.load() || buffer == nullptr || length <= 0) return;
    // 安全桌面(锁屏)模式:本进程无法跨桌面注入,原始输入帧 hex 编码经信令
    // 旁路转发上层(Python 转发 SYSTEM worker 注入锁屏画面)。
    if (secure_mode_.load()) {
      if (cbs.on_signal) {
        cbs.on_signal("secure_input",
                      "{\"data\":\"" + HexEncode(buffer, length) + "\"}");
      }
      return;
    }
    uint32_t seq = 0;
    std::vector<cr::wire::InputEvent> events;
    if (!cr::wire::DecodeInputFrame(reinterpret_cast<const uint8_t*>(buffer),
                                    static_cast<size_t>(length), &seq, &events)) {
      return;
    }
    for (const auto& e : events) ApplyInput(e);
  }

  // ---------------- 输入分发(与 Python session._apply_input 一致) ----------------
  void ApplyInput(const cr::wire::InputEvent& e) {
    if (injector == nullptr) return;
    if (auto key = cr::wire::AsKey(e)) {
      const auto& fnv_to_code = cr::wire::FnvToCode();
      auto it = fnv_to_code.find(key->code);
      if (it != fnv_to_code.end()) {
        uint16_t vk = cr::wire::CodeToVk(it->second);
        if (vk != 0) injector->Key(vk, key->down);
      }
    } else if (auto mv = cr::wire::AsMouseMove(e)) {
      if (mv->dx != 0 || mv->dy != 0) injector->MouseMoveRel(mv->dx, mv->dy);
      if (mv->x >= 0 && mv->y >= 0 && source != nullptr)
        injector->MouseMove(static_cast<int>(mv->x * source->width()),
                            static_cast<int>(mv->y * source->height()));
    } else if (auto btn = cr::wire::AsMouseButton(e)) {
      injector->MouseButton(btn->button, btn->down);
    } else if (auto wh = cr::wire::AsWheel(e)) {
      injector->Wheel(static_cast<int>(wh->dy * 120));
    } else if (auto tc = cr::wire::AsTouch(e)) {
      ApplyTouch(*tc);
    } else if (auto txt = cr::wire::AsText(e)) {
      injector->Text(*txt);
    }
    // GAMEPAD/CUSTOM:v1 注入器不支持
  }

  // ---------------- 触摸手势→鼠标注入(移植 Python _apply_touch_sync) --------
  // 手势约定(noVNC 惯例,触摸屏/平板直接可用):
  //  - 单指:tap=左键点击,拖动=左键拖拽;
  //  - 双指:上下滑动=滚轮滚动(取两指中点位移);
  //  - 三指:tap=右键;
  //  - 手指数变化时切换手势:双指滚屏后抬起一指,剩余单指仅悬停(避免误点);
  //    三指抬起一指回到双指时重新进入滚动。
  void ApplyTouch(const cr::wire::TouchData& t) {
    if (injector == nullptr || source == nullptr) return;
    const int sw = source->width();
    const int sh = source->height();
    if (sw <= 0 || sh <= 0) return;

    auto move_at = [&](float xx, float yy) {
      injector->MouseMove(static_cast<int>(xx * sw), static_cast<int>(yy * sh));
    };
    auto mid2 = [&]() -> std::pair<float, float> {
      float xs = 0.0f, ys = 0.0f;
      int n = 0;
      for (const auto& kv : touches_) {
        if (n >= 2) break;
        xs += kv.second.first;
        ys += kv.second.second;
        ++n;
      }
      return {xs / 2.0f, ys / 2.0f};
    };

    const size_t n0 = touches_.size();
    if (t.phase == 2 || t.phase == 3) {  // up / cancel
      touches_.erase(t.id);
    } else {
      touches_[t.id] = {t.x, t.y};
      if (t.phase == 0) move_at(t.x, t.y);  // 按下时先把光标定位到触点
    }
    const size_t n = touches_.size();

    if (n != n0) {
      // ---- 手指数变化:切换手势状态 ----
      if (touch_right_down_ && n < 3) {
        touch_right_down_ = false;
        injector->MouseButton(2, false);
      }
      if (n == 0) {
        if (touch_left_down_) {
          touch_left_down_ = false;
          injector->MouseButton(0, false);
        }
        touch_scroll_ = false;
        touch_scroll_mid_.reset();
      } else if (n == 1) {
        touch_scroll_ = false;
        touch_scroll_mid_.reset();
        if (n0 == 0) {  // 全新单指:按下左键
          touch_left_down_ = true;
          injector->MouseButton(0, true);
        }
        // 双指回落单指:仅悬停,不再按左键(防滚屏后误点)
      } else if (n == 2) {
        if (touch_left_down_) {
          touch_left_down_ = false;
          injector->MouseButton(0, false);
        }
        touch_scroll_ = true;
        touch_scroll_mid_ = mid2();
      } else {  // n >= 3
        touch_scroll_ = false;
        touch_scroll_mid_.reset();
        if (!touch_right_down_) {
          touch_right_down_ = true;
          injector->MouseButton(2, true);
        }
      }
    } else if (t.phase == 1) {
      // ---- 手指数不变,仅移动 ----
      if (n == 1 && touch_left_down_) {
        move_at(t.x, t.y);
      } else if (n == 2 && touch_scroll_ && touch_scroll_mid_) {
        const auto mid = mid2();
        const float dy = (touch_scroll_mid_->second - mid.second) * sh;
        touch_scroll_mid_ = mid;
        if (std::fabs(dy) >= 1.0f) {
          injector->Wheel(static_cast<int>(dy));  // 指上移=向上滚
        }
      }
    }
  }

  // ---------------- 帧注入:BGRA → I420 → RTCVideoSource ----------------
  void PushFrame(const cr::source::Frame& f) {
    if (video_source_ == nullptr) return;
    const int w = f.width & ~1;  // 编码器要求偶数尺寸(截取左上角)
    const int h = f.height & ~1;
    if (w <= 0 || h <= 0) return;
    const int stride = f.stride > 0 ? f.stride : f.width * 4;
    if (stride < w * 4) return;
    const size_t need =
        static_cast<size_t>(stride) * (h - 1) + static_cast<size_t>(w) * 4;
    if (f.bgra.size() < need) return;

    const int chroma_w = w / 2;
    const int chroma_h = h / 2;
    std::vector<uint8_t> plane_y(static_cast<size_t>(w) * h);
    std::vector<uint8_t> plane_u(static_cast<size_t>(chroma_w) * chroma_h);
    std::vector<uint8_t> plane_v(static_cast<size_t>(chroma_w) * chroma_h);
    BgraToI420(f.bgra.data(), stride, w, h, plane_y.data(), w, plane_u.data(),
               chroma_w, plane_v.data(), chroma_w);

    // facade 的 Create 从指针构造帧数据(内部持有拷贝),局部缓冲可安全释放
    libwebrtc::scoped_refptr<libwebrtc::RTCVideoFrame> frame =
        libwebrtc::RTCVideoFrame::Create(w, h, plane_y.data(), w, plane_u.data(),
                                         chroma_w, plane_v.data(), chroma_w);
    if (frame.get() != nullptr) {
      video_source_->OnCapturedFrame(frame);
      frames_pushed_.fetch_add(1, std::memory_order_relaxed);
    }
  }

  // ---------------- 事件通道:toast 提示 ----------------
  /// 经 events 数据通道发一条 toast(对齐 Python _send_toast);未连接/
  /// 通道未就绪返回 false。
  bool SendToast(const std::string& text) {
    if (closed_.load() || !connected_.load()) return false;
    const auto dc = dc_events_;  // 副本:避免与 OnDataChannel 赋值竞争
    if (dc == nullptr || dc->state() != libwebrtc::RTCDataChannelOpen) {
      return false;
    }
    const uint32_t seq = events_seq_.fetch_add(1) + 1;
    const auto frame =
        cr::wire::EncodeEventFrame({cr::wire::EvToast(text, NowMs())}, seq);
    if (frame.empty()) return false;
    dc->Send(frame.data(), static_cast<uint32_t>(frame.size()), true);
    return true;
  }

  // ---------------- 安全桌面(锁屏)模式 ----------------
  /// 切换安全桌面模式(幂等,任意线程可调)。
  void SetSecureMode(bool on) {
    if (secure_mode_.exchange(on) != on) {
      std::fprintf(stderr, "[cloudrender] secure mode %s\n", on ? "on" : "off");
    }
    if (!on && source != nullptr && !closed_.load()) {
      // 解锁退出安全桌面:强制重启抓屏源。锁屏切换(安全桌面↔Default)会使
      // DXGI duplication 失效,部分错误不自愈导致永久停产(浏览器画面冻结
      // 在密码界面);这里显式 Stop/Start 带重试兜底。
      source->Stop();
      bool ok = false;
      for (int i = 0; i < 5 && !ok; ++i) {
        ok = source->Start();
        if (!ok) std::this_thread::sleep_for(std::chrono::milliseconds(400));
      }
      std::fprintf(stderr,
                   "[cloudrender] capture source restart on unlock: %s\n",
                   ok ? "ok" : "FAILED");
    }
  }

  /// 注入一帧安全桌面图像(BGRA32;数据立即拷贝,上层可随即释放)。
  void PushSecureFrame(const uint8_t* bgra, int width, int height, int stride,
                       uint64_t pts_us) {
    if (bgra == nullptr || width <= 0 || height <= 0) return;
    const int s = stride > 0 ? stride : width * 4;
    if (s < width * 4) return;
    cr::source::Frame f;
    f.width = width;
    f.height = height;
    f.stride = s;
    f.pts_us = pts_us;
    f.bgra.assign(bgra, bgra + static_cast<size_t>(s) * height);
    std::lock_guard<std::mutex> lock(secure_mu_);
    secure_frame_ = std::move(f);
    has_secure_frame_ = true;
  }

  // ---------------- 帧泵 ----------------
  void StartStreaming() {
    if (sender_.joinable() || closed_.load()) return;
    if (!timer_acquired_.exchange(true)) AcquireTimerResolution();
    sender_ = std::thread([this] {
      // 微秒级周期:毫秒整数截断(30fps→33ms)会丢约 1% 帧率;绝对时间基准
      // sleep_until 防累积漂移,末尾短自旋对齐抵消睡眠溢出(至少多醒 ~1ms)。
      const std::chrono::microseconds period(1000000 / std::max(1, config.fps));
      auto next_tick = std::chrono::steady_clock::now();
      cr::source::Frame last;  // 最后一帧:静止时重复推送(见循环内注释)
      bool has_last = false;
      while (!closed_.load()) {
        if (connected_.load() && video_source_ != nullptr) {
          next_tick += period;
          cr::source::Frame f;
          if (secure_mode_.load()) {
            // 安全桌面(锁屏):本机抓屏被系统阻断,取上层注入的锁屏帧
            // (Python 经 SYSTEM worker 抓取);无新帧时重复最后一帧。
            std::lock_guard<std::mutex> lock(secure_mu_);
            if (has_secure_frame_) {
              f = std::move(secure_frame_);
              has_secure_frame_ = false;
            }
          } else if (source != nullptr) {
            source->TryTakeLatest(&f);
          }
          if (!f.bgra.empty()) {
            last = std::move(f);
            has_last = true;
          }
          // 桌面静止(DXGI 无新帧)时重复推最后帧:发送统计恒为配置帧率,
          // 与 8080(aiortc)行为一致;静止帧的重复编码输出极小(全 skip 宏块)。
          if (has_last) PushFrame(last);
          const auto now = std::chrono::steady_clock::now();
          if (next_tick > now) {
            const auto coarse = next_tick - std::chrono::milliseconds(1);
            if (coarse > now) std::this_thread::sleep_until(coarse);
            while (!closed_.load() &&
                   std::chrono::steady_clock::now() < next_tick) {
              std::this_thread::yield();
            }
          } else {
            next_tick = now;  // 本轮超时(抓屏/转换偶发卡顿):重基准不追帧
          }
        } else {
          std::this_thread::sleep_for(std::chrono::milliseconds(10));
          next_tick = std::chrono::steady_clock::now();
        }
      }
    });
  }

  // ---------------- 关闭(幂等) ----------------
  void Close() {
    if (closed_.exchange(true)) return;
    if (timer_acquired_.exchange(false)) ReleaseTimerResolution();
    if (sender_.joinable()) {
      if (sender_.get_id() == std::this_thread::get_id()) {
        sender_.detach();
      } else {
        sender_.join();
      }
    }
    if (dc_input_ != nullptr) dc_input_->UnregisterObserver();
    if (pc_ != nullptr) {
      pc_->DeRegisterRTCPeerConnectionObserver();
      pc_->Close();
    }
    if (source != nullptr) source->Stop();

    dc_input_ = nullptr;
    dc_events_ = nullptr;
    rtp_sender_ = nullptr;
    video_track_ = nullptr;
    video_source_ = nullptr;
    offer_constraints_ = nullptr;
    pc_ = nullptr;
    factory_ = nullptr;  // 释放顺序:会话对象 → 工厂 → 全局 Terminate(析构中)
  }
};

// ----------------------------------------------------------------------
PeerSession::PeerSession(cr::source::FrameSource* source,
                         cr::source::InputInjector* injector,
                         const PeerSessionConfig& config, Callbacks callbacks)
    : impl_(new Impl(source, injector, config, std::move(callbacks))) {
  impl_->Init();
}

PeerSession::~PeerSession() { impl_->Close(); }

bool PeerSession::CreateOffer(std::string* sdp_out) {
  return impl_ != nullptr && impl_->CreateOffer(sdp_out);
}

void PeerSession::HandleSignal(const std::string& type, const std::string& payload_json) {
  if (impl_ != nullptr) impl_->HandleSignal(type, payload_json);
}

bool PeerSession::connected() const {
  return impl_ != nullptr && impl_->connected_.load();
}

bool PeerSession::valid() const {
  return impl_ != nullptr && impl_->init_ok_;
}

void PeerSession::StartStreaming() {
  if (impl_ != nullptr) impl_->StartStreaming();
}

bool PeerSession::QueryStats(uint64_t* bytes_sent, uint64_t* frames_pushed) {
  return impl_ != nullptr && impl_->QueryStats(bytes_sent, frames_pushed);
}

bool PeerSession::SetBitrate(int bitrate_bps) {
  return impl_ != nullptr && impl_->SetBitrate(bitrate_bps);
}

void PeerSession::SetSecureMode(bool on) {
  if (impl_ != nullptr) impl_->SetSecureMode(on);
}

void PeerSession::PushSecureFrame(const uint8_t* bgra, int width, int height,
                                  int stride, uint64_t pts_us) {
  if (impl_ != nullptr) {
    impl_->PushSecureFrame(bgra, width, height, stride, pts_us);
  }
}

bool PeerSession::SendToast(const std::string& text) {
  return impl_ != nullptr && impl_->SendToast(text);
}

void PeerSession::Close() {
  if (impl_ != nullptr) impl_->Close();
}

}  // namespace cr::session

#else  // !CR_HAVE_WEBRTC —— 桩实现(未链接 libwebrtc 时保持头文件可用)

namespace cr::session {
struct PeerSession::Impl {};
PeerSession::PeerSession(cr::source::FrameSource*, cr::source::InputInjector*,
                         const PeerSessionConfig&, Callbacks) {}
PeerSession::~PeerSession() = default;
bool PeerSession::CreateOffer(std::string*) { return false; }
void PeerSession::HandleSignal(const std::string&, const std::string&) {}
bool PeerSession::connected() const { return false; }
bool PeerSession::valid() const { return false; }
void PeerSession::StartStreaming() {}
bool PeerSession::QueryStats(uint64_t*, uint64_t*) { return false; }
bool PeerSession::SetBitrate(int) { return false; }
void PeerSession::SetSecureMode(bool) {}
void PeerSession::PushSecureFrame(const uint8_t*, int, int, int, uint64_t) {}
bool PeerSession::SendToast(const std::string&) { return false; }
void PeerSession::Close() {}
}  // namespace cr::session

#endif  // CR_HAVE_WEBRTC