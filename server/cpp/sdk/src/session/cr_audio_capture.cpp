/**
 * CloudRender 系统音频采集(WASAPI loopback)实现
 *
 * 采集默认播放设备的混音(桌面音频),转 16-bit PCM 立体声并按 10ms 分块
 * 回调。采集线程自带 COM(MTA)初始化;设备失效(切换/拔出/独占)时按秒
 * 重新枚举默认设备自动恢复,直至 Stop。
 */
#include "cloudrender/session/cr_audio_capture.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <audioclient.h>
#include <ksmedia.h>
#include <mmdeviceapi.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>
#include <utility>
#include <vector>

namespace cr::audio {
namespace {

constexpr size_t kOutChannels = 2;  // 统一输出立体声(WebRTC 音频源常用)
constexpr int kChunkMs = 10;        // WebRTC 音频源的帧节奏

void LogHr(const char* stage, HRESULT hr) {
  std::fprintf(stderr, "[cloudrender] audio capture %s failed: 0x%08lx\n",
               stage, static_cast<unsigned long>(hr));
}

bool IsFloatFormat(const WAVEFORMATEX* fmt) {
  if (fmt == nullptr) return false;
  if (fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) return true;
  if (fmt->wFormatTag != WAVE_FORMAT_EXTENSIBLE) return false;
  if (fmt->cbSize <
      sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX)) {
    return false;
  }
  const auto* ext = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(fmt);
  return ::IsEqualGUID(ext->SubFormat, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT) !=
         FALSE;
}

int16_t ToPcm16(float v) {
  if (v > 1.0f) v = 1.0f;
  if (v < -1.0f) v = -1.0f;
  return static_cast<int16_t>(v * 32767.0f);
}

/// 把一次 WASAPI 包转成交错立体声 16-bit,追加到 out。
/// 支持共享模式常见混音格式:32-bit float、16/24/32-bit PCM;
/// 多声道只取前两声道(5.1/7.1 的 L/R),单声道复制到双声道。
void AppendStereo16(const BYTE* data, UINT32 frames, const WAVEFORMATEX* fmt,
                    bool silent, std::vector<int16_t>* out) {
  const size_t base = out->size();
  out->resize(base + static_cast<size_t>(frames) * kOutChannels);
  int16_t* dst = out->data() + base;
  if (silent || data == nullptr || fmt == nullptr) {
    std::memset(dst, 0, static_cast<size_t>(frames) * kOutChannels *
                            sizeof(int16_t));
    return;
  }
  const int channels = fmt->nChannels > 0 ? fmt->nChannels : 1;
  const int bits = fmt->wBitsPerSample;
  const bool is_float = IsFloatFormat(fmt);
  auto sample_at = [&](UINT32 frame, int ch) -> float {
    const size_t index =
        static_cast<size_t>(frame) * channels + static_cast<size_t>(ch);
    if (is_float && bits == 32) {
      float v = 0.0f;
      std::memcpy(&v, data + index * 4, sizeof(v));
      return v;
    }
    if (bits == 16) {
      int16_t v = 0;
      std::memcpy(&v, data + index * 2, sizeof(v));
      return static_cast<float>(v) / 32768.0f;
    }
    if (bits == 24) {
      const BYTE* q = data + index * 3;
      int32_t v = static_cast<int32_t>(q[0]) |
                  (static_cast<int32_t>(q[1]) << 8) |
                  (static_cast<int32_t>(q[2]) << 16);
      if ((v & 0x800000) != 0) v |= ~0xFFFFFF;  // 24-bit 符号扩展
      return static_cast<float>(v) / 8388608.0f;
    }
    if (bits == 32) {
      int32_t v = 0;
      std::memcpy(&v, data + index * 4, sizeof(v));
      return static_cast<float>(v) / 2147483648.0f;
    }
    return 0.0f;  // 不支持的位深:静音(共享模式混音格式通常为 float32)
  };
  for (UINT32 f = 0; f < frames; ++f) {
    const float l = sample_at(f, 0);
    const float r = channels >= 2 ? sample_at(f, 1) : l;
    dst[static_cast<size_t>(f) * kOutChannels] = ToPcm16(l);
    dst[static_cast<size_t>(f) * kOutChannels + 1] = ToPcm16(r);
  }
}

/// WASAPI 对象批释放(析构顺序:先停流再释放接口)。
struct WasapiSession {
  IMMDeviceEnumerator* enumerator = nullptr;
  IMMDevice* device = nullptr;
  IAudioClient* client = nullptr;
  IAudioCaptureClient* capture = nullptr;
  WAVEFORMATEX* format = nullptr;

  ~WasapiSession() {
    if (client != nullptr) client->Stop();
    if (capture != nullptr) capture->Release();
    if (client != nullptr) client->Release();
    if (format != nullptr) ::CoTaskMemFree(format);
    if (device != nullptr) device->Release();
    if (enumerator != nullptr) enumerator->Release();
  }
};

}  // namespace

struct LoopbackCapture::Impl {
  std::atomic<bool> running{false};
  std::atomic<bool> stop{false};
  std::thread thread;
  FrameCallback callback;
  std::vector<int16_t> pending;  // 仅采集线程访问
  int pending_rate = 0;

  void ThreadMain();
  bool CaptureSession();
  void DeliverPending();
};

void LoopbackCapture::Impl::DeliverPending() {
  if (pending_rate <= 0 || !callback) {
    pending.clear();
    return;
  }
  const size_t chunk =
      static_cast<size_t>(pending_rate) * kChunkMs / 1000;  // 每声道帧数
  if (chunk == 0) {
    pending.clear();
    return;
  }
  const size_t chunk_samples = chunk * kOutChannels;
  size_t consumed = 0;
  while (pending.size() - consumed >= chunk_samples) {
    callback(pending.data() + consumed, chunk, pending_rate, kOutChannels);
    consumed += chunk_samples;
  }
  if (consumed > 0) {
    pending.erase(pending.begin(), pending.begin() + consumed);
  }
}

bool LoopbackCapture::Impl::CaptureSession() {
  WasapiSession s;
  HRESULT hr = ::CoCreateInstance(
      __uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
      __uuidof(IMMDeviceEnumerator),
      reinterpret_cast<void**>(&s.enumerator));
  if (FAILED(hr)) {
    LogHr("enumerate devices", hr);
    return false;
  }
  hr = s.enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &s.device);
  if (FAILED(hr)) {
    LogHr("open default render device", hr);
    return false;
  }
  hr = s.device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                          reinterpret_cast<void**>(&s.client));
  if (FAILED(hr)) {
    LogHr("activate audio client", hr);
    return false;
  }
  hr = s.client->GetMixFormat(&s.format);
  if (FAILED(hr) || s.format == nullptr) {
    LogHr("get mix format", hr);
    return false;
  }
  // 共享模式 loopback:缓冲由音频引擎决定,采集默认播放设备的混音
  hr = s.client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                            AUDCLNT_STREAMFLAGS_LOOPBACK, 0, 0, s.format,
                            nullptr);
  if (FAILED(hr)) {
    LogHr("initialize audio client", hr);
    return false;
  }
  hr = s.client->GetService(__uuidof(IAudioCaptureClient),
                            reinterpret_cast<void**>(&s.capture));
  if (FAILED(hr)) {
    LogHr("get capture client", hr);
    return false;
  }
  hr = s.client->Start();
  if (FAILED(hr)) {
    LogHr("start audio client", hr);
    return false;
  }

  pending.clear();
  pending_rate = static_cast<int>(s.format->nSamplesPerSec);
  std::fprintf(stderr,
               "[cloudrender] audio capture started: %d Hz, %u ch (loopback)\n",
               pending_rate, static_cast<unsigned>(s.format->nChannels));

  while (!stop.load()) {
    UINT32 packet = 0;
    hr = s.capture->GetNextPacketSize(&packet);
    if (FAILED(hr)) {
      LogHr("read packet size", hr);
      return false;
    }
    if (packet == 0) {
      // 无声时引擎不一定出包:短睡避免空转(15ms 粒度也远小于引擎缓冲)
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
      continue;
    }
    while (packet != 0 && !stop.load()) {
      BYTE* data = nullptr;
      UINT32 frames = 0;
      DWORD flags = 0;
      hr = s.capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
      if (FAILED(hr)) {
        LogHr("read audio packet", hr);
        return false;
      }
      AppendStereo16(data, frames, s.format,
                     (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0, &pending);
      s.capture->ReleaseBuffer(frames);
      hr = s.capture->GetNextPacketSize(&packet);
      if (FAILED(hr)) {
        LogHr("read packet size", hr);
        return false;
      }
    }
    DeliverPending();
  }
  return true;
}

void LoopbackCapture::Impl::ThreadMain() {
  const HRESULT com = ::CoInitializeEx(nullptr, COINIT_MULTITHREADED);
  const bool com_ok = SUCCEEDED(com);
  if (!com_ok) LogHr("CoInitializeEx", com);

  int failures = 0;
  while (!stop.load()) {
    if (CaptureSession()) break;  // 正常停止(stop 已置位)
    if (stop.load()) break;
    // 设备失效(切换/拔出/独占被抢):每秒重试;日志每 30 次一条防刷屏
    if (++failures % 30 == 1) {
      std::fprintf(stderr,
                   "[cloudrender] audio capture unavailable; retrying "
                   "(attempt %d)\n",
                   failures);
    }
    for (int i = 0; i < 20 && !stop.load(); ++i) {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  }
  pending.clear();
  pending_rate = 0;
  if (com_ok) ::CoUninitialize();
  running = false;
  std::fprintf(stderr, "[cloudrender] audio capture stopped\n");
}

LoopbackCapture::LoopbackCapture() : impl_(new Impl()) {}

LoopbackCapture::~LoopbackCapture() { Stop(); }

bool LoopbackCapture::Start(FrameCallback callback) {
  if (impl_ == nullptr) return false;
  if (impl_->running.load()) return true;
  if (impl_->thread.joinable()) impl_->thread.join();
  impl_->stop = false;
  impl_->callback = std::move(callback);
  impl_->running = true;
  try {
    impl_->thread = std::thread([this] { impl_->ThreadMain(); });
  } catch (...) {
    impl_->running = false;
    impl_->callback = nullptr;
    return false;
  }
  return true;
}

void LoopbackCapture::Stop() {
  if (impl_ == nullptr) return;
  impl_->stop = true;
  if (impl_->thread.joinable()) {
    if (impl_->thread.get_id() == std::this_thread::get_id()) {
      impl_->thread.detach();  // 防御:不允许从采集线程内部自等待
    } else {
      impl_->thread.join();
    }
  }
  impl_->running = false;
  impl_->callback = nullptr;
  impl_->pending.clear();
  impl_->pending_rate = 0;
}

bool LoopbackCapture::running() const {
  return impl_ != nullptr && impl_->running.load();
}

}  // namespace cr::audio