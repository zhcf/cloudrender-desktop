// CloudRender nativecore - DXGI Desktop Duplication 捕获实现
#include "cr_capture.hpp"

#include <dwmapi.h>
#include <windowsx.h>

#include <algorithm>
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstring>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "dwmapi.lib")

namespace cloudrender::core {

namespace {

void LogInfo(const char* fmt, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  OutputDebugStringA(buf);
  OutputDebugStringA("\n");
}

}  // namespace

std::string WideToUtf8(const wchar_t* wide) {
  if (!wide) return {};
  int len = ::WideCharToMultiByte(CP_UTF8, 0, wide, -1, nullptr, 0, nullptr, nullptr);
  if (len <= 0) return {};
  std::string out(static_cast<size_t>(len - 1), '\0');
  ::WideCharToMultiByte(CP_UTF8, 0, wide, -1, &out[0], len, nullptr, nullptr);
  return out;
}

DxgiCapture::~DxgiCapture() { Stop(); }

bool DxgiCapture::CreateDeviceAndOutput(int monitor_index) {
  // 进程 DPI 感知,保证 GetWindowRect 与捕获坐标一致
  ::SetProcessDPIAware();

  UINT factory_flags = 0;
#if defined(_DEBUG)
  factory_flags = DXGI_CREATE_FACTORY_DEBUG;
#endif
  ComPtr<IDXGIFactory1> factory;
  if (FAILED(::CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                  reinterpret_cast<void**>(factory.GetAddressOf())))) {
    factory_flags = 0;
    if (FAILED(::CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                    reinterpret_cast<void**>(factory.GetAddressOf())))) {
      LogInfo("[cr_capture] CreateDXGIFactory1 failed");
      return false;
    }
  }

  // 遍历 adapter × output,顺序与系统显示器枚举一致
  int output_index = 0;
  for (UINT a = 0;; ++a) {
    ComPtr<IDXGIAdapter1> adapter;
    if (factory->EnumAdapters1(a, adapter.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
    for (UINT o = 0;; ++o) {
      ComPtr<IDXGIOutput> output;
      if (adapter->EnumOutputs(o, output.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
      if (output_index == monitor_index) {
        adapter_ = adapter;
        if (FAILED(output.As(&output_))) return false;
        return true;
      }
      ++output_index;
    }
  }
  LogInfo("[cr_capture] monitor %d not found (total %d)", monitor_index, output_index);
  return false;
}

bool DxgiCapture::CreateDuplication() {
  D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
  UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
  D3D_FEATURE_LEVEL got = {};
  if (FAILED(::D3D11CreateDevice(adapter_.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, flags, levels,
                                 ARRAYSIZE(levels), D3D11_SDK_VERSION, device_.GetAddressOf(),
                                 &got, context_.GetAddressOf()))) {
    LogInfo("[cr_capture] D3D11CreateDevice failed");
    return false;
  }

  // Desktop Duplication:一个输出同一时刻只允许一个实例
  HRESULT hr = output_->DuplicateOutput(device_.Get(), duplication_.GetAddressOf());
  if (hr == E_ACCESSDENIED) {
    LogInfo("[cr_capture] duplicate denied: another duplication exists or session wall (UAC secure desktop)");
    return false;
  }
  if (FAILED(hr)) {
    LogInfo("[cr_capture] DuplicateOutput failed hr=0x%08X", static_cast<unsigned>(hr));
    return false;
  }

  DXGI_OUTDUPL_DESC desc{};
  duplication_->GetDesc(&desc);
  width_ = static_cast<int>(desc.ModeDesc.Width);
  height_ = static_cast<int>(desc.ModeDesc.Height);
  if (desc.ModeDesc.Format != DXGI_FORMAT_B8G8R8A8_UNORM) {
    LogInfo("[cr_capture] unsupported format %d (expected B8G8R8A8)", desc.ModeDesc.Format);
    duplication_.Reset();
    return false;
  }
  // 监视器原点在虚拟桌面的坐标(DXGI_OUTDUPL_DESC 无此字段,须从 output 取)
  DXGI_OUTPUT_DESC out_desc{};
  if (SUCCEEDED(output_->GetDesc(&out_desc))) {
    desktop_left_ = out_desc.DesktopCoordinates.left;
    desktop_top_ = out_desc.DesktopCoordinates.top;
  }

  D3D11_TEXTURE2D_DESC sd{};
  sd.Width = desc.ModeDesc.Width;
  sd.Height = desc.ModeDesc.Height;
  sd.MipLevels = 1;
  sd.ArraySize = 1;
  sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
  sd.SampleDesc = {1, 0};
  sd.Usage = D3D11_USAGE_STAGING;
  sd.BindFlags = 0;
  sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
  device_->CreateTexture2D(&sd, nullptr, staging_.GetAddressOf());
  return staging_ != nullptr;
}

void DxgiCapture::UpdateWindowRect() {
  if (!crop_.active) return;
  RECT r{};
  ::DwmGetWindowAttribute(crop_.hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof(r));
  if (::IsRectEmpty(&r)) {
    ::GetWindowRect(crop_.hwnd, &r);  // 回退
  }
  crop_.x = r.left;
  crop_.y = r.top;
  crop_.w = r.right - r.left;
  crop_.h = r.bottom - r.top;
  crop_rect_valid_ = crop_.w > 0 && crop_.h > 0;
}

bool DxgiCapture::TryInitialize(int monitor_index, uint64_t window_hwnd) {
  current_monitor_ = monitor_index;
  if (!CreateDeviceAndOutput(monitor_index)) return false;
  if (!CreateDuplication()) return false;
  if (window_hwnd != 0) {
    crop_.active = true;
    crop_.hwnd = reinterpret_cast<HWND>(window_hwnd);
    UpdateWindowRect();
  }
  return width_ > 0 && height_ > 0;
}

void DxgiCapture::DeliverCropped(const uint8_t* src, uint32_t src_stride, uint32_t cx,
                                 uint32_t cy, uint32_t cw, uint32_t ch, uint64_t pts_us) {
  // cx/cy 为监视器坐标系;裁剪并拷贝到 scratch(逐行)
  const size_t need = static_cast<size_t>(cw) * ch * 4;
  if (scratch_.size() != need) scratch_.resize(need);
  const uint8_t* row = src + static_cast<size_t>(cy) * src_stride + cx * 4;
  for (uint32_t i = 0; i < ch; ++i) {
    std::memcpy(scratch_.data() + static_cast<size_t>(i) * cw * 4, row, size_t{cw} * 4);
    row += src_stride;
  }
  cr_frame frame{};
  frame.width = cw;
  frame.height = ch;
  frame.stride = cw * 4;
  frame.pts_us = pts_us;
  frame.data = scratch_.data();
  frame.is_new = 1;
  cb_(cb_user_, &frame);
}

bool DxgiCapture::ProcessFrame(IDXGIResource* acquired, uint64_t pts_us) {
  // staging 拷贝(帧在 BGRA top-down,直接可交付)
  if (!staging_) return false;
  ComPtr<ID3D11Texture2D> src_tex;   // acquired 是 IDXGIResource,须转为 D3D 纹理
  if (FAILED(acquired->QueryInterface(IID_PPV_ARGS(src_tex.GetAddressOf())))) return false;
  context_->CopyResource(staging_.Get(), src_tex.Get());
  D3D11_MAPPED_SUBRESOURCE mapped{};
  if (FAILED(context_->Map(staging_.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return false;
  const auto* pixels = static_cast<const uint8_t*>(mapped.pData);
  if (!crop_.active || !crop_rect_valid_) {
    cr_frame frame{};
    frame.width = static_cast<uint32_t>(width_);
    frame.height = static_cast<uint32_t>(height_);
    frame.stride = static_cast<uint32_t>(mapped.RowPitch);
    frame.pts_us = pts_us;
    frame.data = pixels;
    frame.is_new = 1;
    cb_(cb_user_, &frame);
  } else {
    // 裁剪到监视器内、与窗口矩形相交
    int64_t x = crop_.x - desktop_left_;
    int64_t y = crop_.y - desktop_top_;
    int64_t x2 = x + crop_.w, y2 = y + crop_.h;
    x = std::max<int64_t>(x, 0);
    y = std::max<int64_t>(y, 0);
    x2 = std::min<int64_t>(x2, width_);
    y2 = std::min<int64_t>(y2, height_);
    if (x2 > x && y2 > y) {
      DeliverCropped(pixels, static_cast<uint32_t>(mapped.RowPitch),
                     static_cast<uint32_t>(x), static_cast<uint32_t>(y),
                     static_cast<uint32_t>(x2 - x), static_cast<uint32_t>(y2 - y), pts_us);
    }
  }
  context_->Unmap(staging_.Get(), 0);
  return true;
}

void DxgiCapture::Pump() {
  LARGE_INTEGER freq{};
  ::QueryPerformanceFrequency(&freq);
  const uint32_t timeout_ms =
      frame_interval_us_ > 0 ? static_cast<uint32_t>(std::min(50, frame_interval_us_ / 1000)) : 16;
  UINT frame_counter = 0;
  bool need_first = true;  // 重建(首次/ACCESS_LOST)后强制交付首帧

  while (!stop_.load()) {
    if (!duplication_) {  // ACCESS_LOST 后重建
      if (stop_.load()) break;
      if (!CreateDeviceAndOutput(current_monitor_)) {
        ::Sleep(200);
        continue;
      }
      if (!CreateDuplication()) {
        ::Sleep(200);
        continue;
      }
      need_first = true;  // 新 duplication:当前桌面镜像无 Present 事件
    }
    ComPtr<IDXGIResource> acquired;
    DXGI_OUTDUPL_FRAME_INFO info{};
    HRESULT hr = duplication_->AcquireNextFrame(timeout_ms, &info, acquired.GetAddressOf());
    if (FAILED(hr)) {
      if (hr != DXGI_ERROR_WAIT_TIMEOUT) {
        // ACCESS_LOST / 其他错误(如 INVALID_CALL):重建 duplication。
        // 锁屏切换后部分错误不自愈,不重建则永久停产(解锁后画面冻结)。
        LogInfo("[cr_capture] acquire hr=0x%08X, rebuild duplication",
                static_cast<unsigned>(hr));
        duplication_.Reset();  // 触发重建并保留 > 设备
        ::Sleep(100);
      }
      // WAIT_TIMEOUT:继续等待
      continue;
    }
    if (acquired) {
      LARGE_INTEGER now{};
      ::QueryPerformanceCounter(&now);
      uint64_t pts_us = static_cast<uint64_t>(now.QuadPart) * 1000000ull /
                        static_cast<uint64_t>(freq.QuadPart);
      if (need_first ||
          (info.LastPresentTime.QuadPart != 0 && info.LastPresentTime.QuadPart != last_pts_)) {
        // 重建后的首帧 LastPresentTime 可能为 0(仅桌面镜像,无新 Present);
        // 不过滤交付,否则静态桌面(如锁屏画面)重建后将永远无帧输出。
        need_first = false;
        if (++frame_counter % 30 == 0) UpdateWindowRect();  // 窗口移动时跟随
        ProcessFrame(acquired.Get(), pts_us);
        last_pts_ = info.LastPresentTime.QuadPart;
      }
      acquired.Reset();
      duplication_->ReleaseFrame();
    }
  }
}

bool DxgiCapture::Start(cr_frame_cb cb, void* user, int max_fps) {
  if (running_.load()) return true;
  cb_ = cb;
  cb_user_ = user;
  frame_interval_us_ = max_fps > 0 ? 1000000 / max_fps : 0;
  stop_.store(false);
  // 重建 device/duplication:duplication 需要线程外也可创建,故复用现有;
  // ACCESS_LOST 时 Pump 自行重建(记录当前 monitor 用于重建)
  thread_ = std::thread([this] { Pump(); });
  running_.store(true);
  return true;
}

void DxgiCapture::Stop() {
  if (!running_.exchange(false)) return;
  stop_.store(true);
  if (thread_.joinable()) thread_.join();
}

}  // namespace cloudrender::core