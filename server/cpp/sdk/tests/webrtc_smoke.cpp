/**
 * WebRTC 会话层冒烟测试
 *
 * 不依赖真实桌面/nativecore:用假帧源与假注入器驱动 cr::session::PeerSession,
 * 验证 libwebrtc facade 初始化、PeerConnection 建立与 offer 生成链路:
 *  - CreateOffer 成功且 SDP 含 m=video(视频轨) 与 m=application(DataChannel)
 *  - on_signal 首条信令为 offer
 *
 * 退出码:0=通过;1=CreateOffer 失败;2=缺少 m=video;3=缺少 m=application;4=信令类型不符。
 */
#include <cstdio>
#include <cstdint>
#include <string>
#include <string_view>

#include "cloudrender/session/cr_webrtc_session.hpp"

namespace {

class FakeFrameSource final : public cr::source::FrameSource {
 public:
  int width() const override { return kWidth; }
  int height() const override { return kHeight; }

  bool Start() override {
    started_ = true;
    return true;
  }

  void Stop() override { started_ = false; }

  bool TryTakeLatest(cr::source::Frame* out) override {
    if (!started_ || !has_frame_) return false;
    has_frame_ = false;
    const int stride = kWidth * 4;
    out->width = kWidth;
    out->height = kHeight;
    out->stride = stride;
    out->pts_us = ++pts_us_;
    out->bgra.assign(static_cast<size_t>(stride) * kHeight, 0x7f);
    return true;
  }

 private:
  static constexpr int kWidth = 640;
  static constexpr int kHeight = 480;
  bool started_ = false;
  bool has_frame_ = true;
  uint64_t pts_us_ = 0;
};

class FakeInjector final : public cr::source::InputInjector {
 public:
  bool Key(uint16_t, bool) override { return true; }
  bool MouseMove(int, int) override { return true; }
  bool MouseMoveRel(int, int) override { return true; }
  bool MouseButton(int, bool) override { return true; }
  bool Wheel(int) override { return true; }
  bool Text(std::string_view) override { return true; }
};

}  // namespace

int main() {
  FakeFrameSource source;
  FakeInjector injector;

  cr::session::PeerSessionConfig config;
  config.ice_servers.clear();  // 冒烟:不需要 STUN,本地生成 offer 即可
  config.fps = 5;

  int signal_count = 0;
  std::string first_signal_type;
  cr::session::PeerSession::Callbacks callbacks;
  callbacks.on_signal = [&](const std::string& type, const std::string& payload) {
    ++signal_count;
    if (first_signal_type.empty()) first_signal_type = type;
    std::printf("[signal] %s payload=%zuB\n", type.c_str(), payload.size());
  };
  callbacks.on_closed = [](const char* reason) {
    std::printf("[closed] %s\n", reason ? reason : "(null)");
  };

  cr::session::PeerSession session(&source, &injector, config, callbacks);
  std::string sdp;
  const bool ok = session.CreateOffer(&sdp);

  const bool has_video = sdp.find("m=video") != std::string::npos;
  const bool has_data = sdp.find("m=application") != std::string::npos;
  std::printf("CreateOffer=%d sdp=%zuB video=%d app=%d signals=%d\n",
              ok ? 1 : 0, sdp.size(), has_video ? 1 : 0, has_data ? 1 : 0,
              signal_count);

  int rc = 0;
  if (!ok || sdp.empty()) {
    std::printf("SMOKE_FAIL create_offer\n");
    rc = 1;
  } else if (!has_video) {
    std::printf("SMOKE_FAIL no_video_mline\n");
    rc = 2;
  } else if (!has_data) {
    std::printf("SMOKE_FAIL no_application_mline\n");
    rc = 3;
  } else if (first_signal_type != "offer") {
    std::printf("SMOKE_FAIL signal_type=%s\n", first_signal_type.c_str());
    rc = 4;
  } else {
    std::printf("SMOKE_OK\n");
  }

  session.Close();
  return rc;
}