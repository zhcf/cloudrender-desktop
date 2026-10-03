// CloudRender nativecore - DXGI Desktop Duplication 捕获
#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "cloudrender/capi/cr_api.h"

namespace cloudrender::core {

using Microsoft::WRL::ComPtr;

// 窗口标题等宽字符转 UTF-8
std::string WideToUtf8(const wchar_t* wide);

// ---- 各由一个实例守卫 DXGI Duplication(同输出只允许一个) ----
class DxgiCapture final {
 public:
  ~DxgiCapture();

  bool TryInitialize(int monitor_index, uint64_t window_hwnd);
  // start:线程内 pump;回调在捕获线程调用(不可阻塞)
  bool Start(cr_frame_cb cb, void* user, int max_fps);
  void Stop();
  bool WantsCrop() const { return crop_.active; }
  int Width() const { return width_; }
  int Height() const { return height_; }

 private:
  bool CreateDeviceAndOutput(int monitor_index);
  bool CreateDuplication();
  bool ProcessFrame(IDXGIResource* acquired, uint64_t pts_us);
  void Pump();
  void DeliverCropped(const uint8_t* src, uint32_t src_stride, uint32_t crop_x,
                      uint32_t crop_y, uint32_t crop_w, uint32_t crop_h,
                      uint64_t pts_us);
  void UpdateWindowRect();

  // 生命周期由 Pump 线程管理
  ComPtr<IDXGIAdapter1> adapter_;
  ComPtr<IDXGIOutput1> output_;
  ComPtr<ID3D11Device> device_;
  ComPtr<ID3D11DeviceContext> context_;
  ComPtr<IDXGIOutputDuplication> duplication_;
  ComPtr<ID3D11Texture2D> staging_;

  int width_ = 0;
  int height_ = 0;
  int desktop_left_ = 0;   // 监视器原点在虚拟屏幕上坐标
  int desktop_top_ = 0;
  int current_monitor_ = 0;  // 用于 ACCESS_LOST 重建
  int64_t last_pts_ = 0;

  struct Crop {
    bool active = false;
    HWND hwnd = nullptr;
    int x = 0, y = 0, w = 0, h = 0;
  } crop_;

  std::vector<uint8_t> scratch_;   // 裁剪帧缓冲
  std::vector<uint8_t> fallback_;  // 非 BGRA 回退格式的转换缓冲

  std::thread thread_;
  std::atomic<bool> running_{false};
  std::atomic<bool> stop_{false};
  cr_frame_cb cb_ = nullptr;
  void* cb_user_ = nullptr;
  int frame_interval_us_ = 0;

  bool crop_rect_valid_ = false;
};

}  // namespace cloudrender::core