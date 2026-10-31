/**
 * CloudRender 系统音频采集(WASAPI loopback)
 *
 * 采集默认播放设备上的系统混音(桌面音频),统一转成 16-bit PCM 立体声,
 * 按 10ms 一块回调——对齐 WebRTC 自定义音频源(RTCAudioSource::CaptureFrame)
 * 的帧节奏;设备被占用/切换/移除时自动重新枚举默认设备并恢复采集。
 */
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>

namespace cr::audio {

class LoopbackCapture {
 public:
  /// 回调:pcm 为交错立体声 16-bit 采样,frames 为每声道帧数(约 rate/100)。
  using FrameCallback = std::function<void(const int16_t* pcm, size_t frames,
                                           int sample_rate, size_t channels)>;

  LoopbackCapture();
  ~LoopbackCapture();

  LoopbackCapture(const LoopbackCapture&) = delete;
  LoopbackCapture& operator=(const LoopbackCapture&) = delete;

  /// 启动采集线程(默认播放设备)。已在运行返回 true;线程创建失败返回 false。
  /// 设备打开失败不算失败:线程内每秒重试(无音频设备时保持静默等待)。
  bool Start(FrameCallback callback);

  /// 停止并等待采集线程退出(幂等)。返回后回调不会再触发。
  void Stop();

  bool running() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace cr::audio