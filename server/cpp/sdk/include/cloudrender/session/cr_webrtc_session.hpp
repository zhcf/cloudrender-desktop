/**
 * CloudRender WebRTC 会话层(C++)
 *
 * 以 libwebrtc 原生 API 实现服务端 offer 会话,直连 nativecore 适配器(零拷贝语义):
 *  - CreateOffer:创建 PeerConnection + 视频轨(截屏源)+ input/events DataChannel + offer
 *  - HandleSignal:answer / ice / disconnect
 *  - StartStreaming:帧泵线程(取最新帧 → BGRA→I420 → VideoTrackSource)
 *  - 输入通道二进制帧经 cr::wire 解码后直接回调注入器
 *
 * 信令传输层(WebSocket 等)由上层接入:本类只产生/消费 JSON 片段,
 * 由 Callbacks::on_signal(type, payload_json) 交给上层发送。
 *
 * 编译要求:预编译 libwebrtc(webrtc-sdk facade 包),CMake 打开 CR_BUILD_WEBRTC,
 * 自动探测 third_party/libwebrtc/libwebrtc-x64-release(可用 -DCR_WEBRTC_ROOT 覆盖)。
 * 未编译该单元时,本头文件仍可用(全部接口返回失败/空实现)。
 */
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "cloudrender/session/cr_source.hpp"

namespace cr::session {

struct PeerSessionConfig {
  std::vector<std::string> ice_servers{"stun:stun.l.google.com:19302"};
  int fps = 30;
  bool prefer_h264 = true;  // offer 编解码顺序 H264 > VP8
  /// 固定目标码率(bps):>0 时 min=max=该值,起步即高码率(对齐 aiortc 版
  /// 固定码率模式,避免带宽估计起步偏低导致前十几秒画面模糊);0=自适应。
  int bitrate_bps = 0;
};

class PeerSession {
 public:
  struct Callbacks {
    /// 信令片段:(type, payload_json) → 上层发往客户端。type ∈ offer/stats/...
    std::function<void(const std::string& type, const std::string& payload_json)> on_signal;
    /// 会话因 ICE failed/closed/disconnected 等原因结束时回调。
    std::function<void(const char* reason)> on_closed;
  };

  PeerSession(cr::source::FrameSource* source, cr::source::InputInjector* injector,
              const PeerSessionConfig& config, Callbacks callbacks);
  ~PeerSession();

  PeerSession(const PeerSession&) = delete;
  PeerSession& operator=(const PeerSession&) = delete;

  /// 建立会话并生成 offer(本地描述已设置)。成功返回 true,sdp_out 为 SDP 文本。
  bool CreateOffer(std::string* sdp_out);

  /// 处理客户端信令:type ∈ answer / ice / disconnect;payload_json 为 payload 对象的 JSON。
  void HandleSignal(const std::string& type, const std::string& payload_json);

  /// ICE/DTLS 已连接(可开始推流)。
  bool connected() const;

  /// 初始化是否成功(libwebrtc + 抓屏源;构造后判定会话是否可用)。
  bool valid() const;

  /// 启动帧泵线程(幂等)。
  void StartStreaming();

  /// 查询发送侧累计统计(outbound-rtp 的 bytesSent + 帧泵累计推送帧数,
  /// 上层按两次采样差分算 fps/码率)。成功返回 true;未连接/超时返回 false。
  bool QueryStats(uint64_t* bytes_sent, uint64_t* frames_pushed);

  /// 设置视频目标码率(bps;min=max 固定)。运行期任意线程可调,幂等;
  /// bps<=0/未就绪/提交失败返回 false。connected 后周期重申效果最佳。
  bool SetBitrate(int bitrate_bps);

  /// 安全桌面(锁屏)模式:开启后帧泵改推 PushSecureFrame 注入的帧(普通
  /// 权限进程锁屏时无法抓屏),输入帧经 on_signal("secure_input",
  /// {"data":"<hex>"}) 旁路转发上层(由 SYSTEM worker 注入安全桌面);
  /// 关闭后恢复本机抓屏/注入。任意线程可调,幂等。
  void SetSecureMode(bool on);

  /// 注入一帧安全桌面图像(BGRA32,行字节=stride)。数据被立即拷贝,
  /// 上层可随即释放;应在 SetSecureMode(true) 期间按帧率重复调用。
  void PushSecureFrame(const uint8_t* bgra, int width, int height, int stride,
                       uint64_t pts_us);

  /// 经 events 数据通道发送一条 toast 提示(UTF-8)。未连接/通道未就绪
  /// 返回 false。
  bool SendToast(const std::string& text);

  /// 关闭会话(释放 PeerConnection 与帧泵)。
  void Close();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace cr::session