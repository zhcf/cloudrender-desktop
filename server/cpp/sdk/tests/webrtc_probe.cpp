/**
 * WebRTC facade 分步探针(诊断工具)
 *
 * 逐步调用 webrtc-sdk facade API 并即时打印(fflush 保证不丢输出),
 * 用于定位 libwebrtc.dll 内崩溃的确切调用点。
 * 输出形如:[step] N 描述 / -> 结果。最后正常输出 PROBE_DONE。
 */
#include <cstdio>
#include <chrono>
#include <cstdint>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#pragma comment(lib, "psapi.lib")
#endif

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

#define STEP(msg)                                 \
  do {                                            \
    std::printf("[step] %s\n", (msg));            \
    std::fflush(stdout);                          \
  } while (0)

#define RESULT(fmt, ...)                          \
  do {                                            \
    std::printf("    -> " fmt "\n", __VA_ARGS__); \
    std::fflush(stdout);                          \
  } while (0)

namespace {

// ----------------------------------------------------------------------
// 崩溃捕获:免调试器定位 libwebrtc 崩溃点
// 打印:异常码 / 错误 RIP(模块 RVA) / 非法访问地址(读或写) /
//       栈扫描出的返回地址候选(libwebrtc/probe 模块内 RVA)
// ----------------------------------------------------------------------
uintptr_t g_webrtc_base = 0;
uintptr_t g_webrtc_end = 0;
uintptr_t g_self_base = 0;
uintptr_t g_self_end = 0;

LONG WINAPI CrashHandler(EXCEPTION_POINTERS* ep) {
  const DWORD code = ep->ExceptionRecord->ExceptionCode;
  const uintptr_t fault =
      reinterpret_cast<uintptr_t>(ep->ExceptionRecord->ExceptionAddress);
  std::printf("\n[CRASH] exception=0x%08lX fault_rip=0x%llX", code,
              static_cast<unsigned long long>(fault));
  if (g_webrtc_base && fault >= g_webrtc_base && fault < g_webrtc_end) {
    std::printf(" rip_rva=0x%llX",
                static_cast<unsigned long long>(fault - g_webrtc_base));
  }
  if (code == EXCEPTION_ACCESS_VIOLATION &&
      ep->ExceptionRecord->NumberParameters >= 2) {
    std::printf(" access=%s fault_addr=0x%llX",
                ep->ExceptionRecord->ExceptionInformation[0] ? "write" : "read",
                static_cast<unsigned long long>(
                    ep->ExceptionRecord->ExceptionInformation[1]));
  }
  std::printf("\n[CRASH] stack scan(低 RVA=代码, 高 RVA=可能是数据/vtable):\n");
  const uintptr_t rsp = static_cast<uintptr_t>(ep->ContextRecord->Rsp);
  int shown = 0;
  for (uintptr_t p = rsp; p < rsp + 0x8000 && shown < 48;
       p += sizeof(uintptr_t)) {
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(reinterpret_cast<LPCVOID>(p), &mbi, sizeof(mbi)) == 0 ||
        mbi.State != MEM_COMMIT ||
        (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
      break;
    }
    const uintptr_t v = *reinterpret_cast<uintptr_t*>(p);
    if (g_webrtc_base && v >= g_webrtc_base && v < g_webrtc_end) {
      const unsigned long long rva =
          static_cast<unsigned long long>(v - g_webrtc_base);
      std::printf("  [%02d] sp+0x%04llX libwebrtc+0x%llX%s\n", shown,
                  static_cast<unsigned long long>(p - rsp), rva,
                  rva < 0x1200000 ? "" : " (数据?)");
      ++shown;
    } else if (g_self_base && v >= g_self_base && v < g_self_end) {
      std::printf("  [%02d] sp+0x%04llX probe+0x%llX\n", shown,
                  static_cast<unsigned long long>(p - rsp),
                  static_cast<unsigned long long>(v - g_self_base));
      ++shown;
    }
  }
  std::printf("[CRASH] end\n");
  std::fflush(stdout);
  return EXCEPTION_EXECUTE_HANDLER;
}

void InstallCrashHandler() {
  const HMODULE webrtc = GetModuleHandleA("libwebrtc.dll");
  if (webrtc != nullptr) {
    MODULEINFO mi{};
    if (GetModuleInformation(GetCurrentProcess(), webrtc, &mi, sizeof(mi))) {
      g_webrtc_base = reinterpret_cast<uintptr_t>(mi.lpBaseOfDll);
      g_webrtc_end = g_webrtc_base + mi.SizeOfImage;
    }
  }
  const HMODULE self = GetModuleHandleA(nullptr);
  if (self != nullptr) {
    MODULEINFO mi{};
    if (GetModuleInformation(GetCurrentProcess(), self, &mi, sizeof(mi))) {
      g_self_base = reinterpret_cast<uintptr_t>(mi.lpBaseOfDll);
      g_self_end = g_self_base + mi.SizeOfImage;
    }
  }
  SetUnhandledExceptionFilter(&CrashHandler);
}

class NullObserver : public libwebrtc::RTCPeerConnectionObserver {
 public:
  void OnSignalingState(libwebrtc::RTCSignalingState) override {}
  void OnPeerConnectionState(libwebrtc::RTCPeerConnectionState) override {}
  void OnIceGatheringState(libwebrtc::RTCIceGatheringState) override {}
  void OnIceConnectionState(libwebrtc::RTCIceConnectionState) override {}
  void OnIceCandidate(libwebrtc::scoped_refptr<libwebrtc::RTCIceCandidate>) override {}
  void OnAddStream(libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream>) override {}
  void OnRemoveStream(libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream>) override {}
  void OnDataChannel(libwebrtc::scoped_refptr<libwebrtc::RTCDataChannel>) override {}
  void OnRenegotiationNeeded() override {}
  void OnTrack(libwebrtc::scoped_refptr<libwebrtc::RTCRtpTransceiver>) override {}
  void OnAddTrack(libwebrtc::vector<libwebrtc::scoped_refptr<libwebrtc::RTCMediaStream>>,
                  libwebrtc::scoped_refptr<libwebrtc::RTCRtpReceiver>) override {}
  void OnRemoveTrack(libwebrtc::scoped_refptr<libwebrtc::RTCRtpReceiver>) override {}
};

}  // namespace

int main() {
  STEP("0 main 进入(说明进程启动/DLL 静态初始化已通过)");
  InstallCrashHandler();
  std::printf("    -> crash handler installed (webrtc_base=0x%llX)\n",
              static_cast<unsigned long long>(g_webrtc_base));
  std::fflush(stdout);

  STEP("1 LibWebRTC::Initialize");
  const bool ok = libwebrtc::LibWebRTC::Initialize();
  RESULT("Initialize=%d", ok ? 1 : 0);

  STEP("2 CreateRTCPeerConnectionFactory");
  auto factory = libwebrtc::LibWebRTC::CreateRTCPeerConnectionFactory();
  RESULT("factory=%p", static_cast<void*>(factory.get()));

  STEP("3 RTCMediaConstraints::Create");
  auto constraints = libwebrtc::RTCMediaConstraints::Create();
  RESULT("constraints=%p", static_cast<void*>(constraints.get()));

  STEP("4 RTCConfiguration 栈上构造 + 设置字段");
  libwebrtc::RTCConfiguration config;
  config.offer_to_receive_video = false;
  config.offer_to_receive_audio = false;
  RESULT("ice_servers[0].uri.size=%d", static_cast<int>(config.ice_servers[0].uri.size()));

  STEP("4.5 factory->Initialize()");
  const bool factory_inited = factory->Initialize();
  RESULT("factory.Initialize=%d", factory_inited ? 1 : 0);

  STEP("5 factory->Create(PeerConnection)");
  auto pc = factory->Create(config, constraints);
  RESULT("pc=%p", static_cast<void*>(pc.get()));

  STEP("6 RegisterRTCPeerConnectionObserver");
  NullObserver observer;
  pc->RegisterRTCPeerConnectionObserver(&observer);
  std::printf("    -> done\n");
  std::fflush(stdout);

  STEP("7 CreateCustomVideoSource");
  auto vsource = factory->CreateCustomVideoSource(
      "probe", libwebrtc::RTCMediaConstraints::Create());
  RESULT("vsource=%p", static_cast<void*>(vsource.get()));

  STEP("8 CreateVideoTrack");
  auto vtrack = factory->CreateVideoTrack(vsource, "probe_track");
  RESULT("vtrack=%p", static_cast<void*>(vtrack.get()));

  STEP("9 AddTrack");
  {
    std::vector<std::string> ids_std{"probe"};
    libwebrtc::vector<libwebrtc::string> ids(ids_std);
    auto sender = pc->AddTrack(vtrack, ids);
    RESULT("sender=%p", static_cast<void*>(sender.get()));
  }

  STEP("10 CreateDataChannel(input)");
  libwebrtc::RTCDataChannelInit dc_init;
  auto dc = pc->CreateDataChannel("input", &dc_init);
  RESULT("dc=%p", static_cast<void*>(dc.get()));

  STEP("11 RTCVideoFrame::Create(I420) + OnCapturedFrame");
  {
    const int w = 64, h = 32, cw = w / 2, ch = h / 2;
    std::vector<uint8_t> py(static_cast<size_t>(w) * h, 128);
    std::vector<uint8_t> pu(static_cast<size_t>(cw) * ch, 128);
    std::vector<uint8_t> pv(static_cast<size_t>(cw) * ch, 128);
    auto frame = libwebrtc::RTCVideoFrame::Create(w, h, py.data(), w, pu.data(),
                                                  cw, pv.data(), cw);
    RESULT("frame=%p", static_cast<void*>(frame.get()));
    if (frame.get() != nullptr && vsource.get() != nullptr) {
      vsource->OnCapturedFrame(frame);
      std::printf("    -> OnCapturedFrame done\n");
      std::fflush(stdout);
    }
  }

  STEP("12 CreateOffer(回调计数,等待 3s)");
  {
    int sdp_cb = 0;
    pc->CreateOffer(
        libwebrtc::OnSdpCreateSuccess(
            [&sdp_cb](const libwebrtc::string sdp, const libwebrtc::string type) {
              sdp_cb = 1;
              std::printf("    [cb] offer sdp=%d type=%s\n",
                          static_cast<int>(sdp.std_string().size()), type.c_string());
              std::fflush(stdout);
            }),
        libwebrtc::OnSdpCreateFailure([](const char* err) {
          std::printf("    [cb] offer failed: %s\n", err ? err : "(null)");
          std::fflush(stdout);
        }),
        constraints);
    for (int i = 0; i < 30 && !sdp_cb; ++i) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    RESULT("sdp_cb=%d", sdp_cb);
  }

  STEP("13 Close + Terminate");
  pc->DeRegisterRTCPeerConnectionObserver();
  pc->Close();
  libwebrtc::LibWebRTC::Terminate();
  std::printf("    -> done\n");
  std::fflush(stdout);

  std::printf("PROBE_DONE\n");
  std::fflush(stdout);
  return 0;
}