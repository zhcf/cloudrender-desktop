// CloudRender nativecore - OS 输入注入(SendInput)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <string>
#include <vector>

#include "cloudrender/capi/cr_api.h"

#pragma comment(lib, "user32.lib")

namespace cloudrender::core {

namespace {

/* extended keys:需要 KEYEVENTF_EXTENDEDKEY 的 VK 列表 */
bool IsExtendedKey(uint16_t vk) {
  return vk == VK_INSERT || vk == VK_DELETE || vk == VK_HOME || vk == VK_END ||
         vk == VK_PRIOR || vk == VK_NEXT || vk == VK_LEFT || vk == VK_RIGHT ||
         vk == VK_UP || vk == VK_DOWN || vk == VK_SNAPSHOT || vk == VK_RCONTROL ||
         vk == VK_RMENU || vk == VK_LWIN || vk == VK_RWIN || vk == VK_NUMLOCK ||
         vk == VK_DIVIDE;
}

int InjectKey(uint16_t vk, bool down) {
  INPUT in{};
  in.type = INPUT_KEYBOARD;
  in.ki.wVk = vk;
  in.ki.wScan = static_cast<WORD>(::MapVirtualKeyW(vk, MAPVK_VK_TO_VSC));
  in.ki.dwFlags = (IsExtendedKey(vk) ? KEYEVENTF_EXTENDEDKEY : 0u) |
                  (down ? 0u : KEYEVENTF_KEYUP);
  return ::SendInput(1, &in, sizeof(in)) == 1 ? CR_OK : CR_E_SYS;
}

}  // namespace

int InjectKeyEvent(cr_inject*, uint16_t vk, int down) {
  return InjectKey(vk, down != 0);
}

int InjectMouseMove(cr_inject*, int32_t x, int32_t y) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE;
  const int vw = ::GetSystemMetrics(SM_CXVIRTUALSCREEN);
  const int vh = ::GetSystemMetrics(SM_CYVIRTUALSCREEN);
  const int vl = ::GetSystemMetrics(SM_XVIRTUALSCREEN);
  const int vt = ::GetSystemMetrics(SM_YVIRTUALSCREEN);
  in.mi.dx = static_cast<LONG>((static_cast<double>(x - vl) / vw) * 65535.0);
  in.mi.dy = static_cast<LONG>((static_cast<double>(y - vt) / vh) * 65535.0);
  return ::SendInput(1, &in, sizeof(in)) == 1 ? CR_OK : CR_E_SYS;
}

int InjectMouseMoveRel(cr_inject*, int32_t dx, int32_t dy) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  in.mi.dwFlags = MOUSEEVENTF_MOVE;
  in.mi.dx = dx;
  in.mi.dy = dy;
  return ::SendInput(1, &in, sizeof(in)) == 1 ? CR_OK : CR_E_SYS;
}

int InjectMouseButton(cr_inject*, int button, int down) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  DWORD flag = 0;
  switch (button) {
    case 0: flag = down ? MOUSEEVENTF_LEFTDOWN : MOUSEEVENTF_LEFTUP; break;
    case 1: flag = down ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_MIDDLEUP; break;
    case 2: flag = down ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP; break;
    default: return CR_E_ARG;
  }
  in.mi.dwFlags = flag;
  return ::SendInput(1, &in, sizeof(in)) == 1 ? CR_OK : CR_E_SYS;
}

int InjectMouseWheel(cr_inject*, int32_t delta) {
  INPUT in{};
  in.type = INPUT_MOUSE;
  in.mi.dwFlags = MOUSEEVENTF_WHEEL;
  in.mi.mouseData = static_cast<DWORD>(delta);
  return ::SendInput(1, &in, sizeof(in)) == 1 ? CR_OK : CR_E_SYS;
}

int InjectText(cr_inject*, const char* utf8) {
  if (!utf8) return CR_E_ARG;
  int len = ::MultiByteToWideChar(CP_UTF8, 0, utf8, -1, nullptr, 0);
  if (len <= 1) return CR_OK;
  std::vector<wchar_t> wide(static_cast<size_t>(len));
  ::MultiByteToWideChar(CP_UTF8, 0, utf8, -1, wide.data(), len);

  std::vector<INPUT> inputs;
  inputs.reserve(wide.size() * 2);
  for (const wchar_t ch : wide) {
    if (ch == 0) break;
    INPUT dn{};
    dn.type = INPUT_KEYBOARD;
    dn.ki.wVk = 0;
    dn.ki.wScan = ch;
    dn.ki.dwFlags = KEYEVENTF_UNICODE;
    inputs.push_back(dn);
    INPUT up = dn;
    up.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
    inputs.push_back(up);
  }
  if (inputs.empty()) return CR_OK;
  return ::SendInput(static_cast<UINT>(inputs.size()), inputs.data(),
                     static_cast<int>(sizeof(INPUT))) == static_cast<UINT>(inputs.size())
             ? CR_OK
             : CR_E_SYS;
}

}  // namespace cloudrender::core