/**
 * CloudRender nativecore - C ABI
 *
 * 桌面捕获 + 输入注入的统一原生核心,可被 C++/Python(ctypes)
 * 及任意语言绑定。所有函数线程安全(除创建/销毁外可在任意线程调用)。
 *
 * 窗口平台:v1 仅 Windows(DXGI Desktop Duplication + SendInput)。
 */
#ifndef CLOUDRENDER_CAPI_CR_API_H
#define CLOUDRENDER_CAPI_CR_API_H

#include <stdint.h>

#ifdef _WIN32
#ifdef CR_NATIVECORE_BUILD
#define CR_API __declspec(dllexport)
#else
#define CR_API __declspec(dllimport)
#endif
#define CR_CALL __cdecl
#else
#define CR_API __attribute__((visibility("default")))
#define CR_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ---------- 基础 ---------- */

enum {
    CR_OK = 0,
    CR_E_ARG = 1,     /* 参数非法 */
    CR_E_STATE = 2,   /* 状态错误(如未 start 就 get_size) */
    CR_E_SYS = 3,     /* 系统调用失败(可通过口 port 见日志) */
    CR_E_NOTSUP = 4   /* 不支持 */
};

/* 帧像素格式:BGRA32,行字节 = stride */
typedef struct cr_frame {
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint64_t pts_us;        /* 捕获时间戳(us),QPC 换算 */
    const uint8_t *data;    /* 仅回调期间有效 */
    uint8_t is_new;         /* 恒为 1,保留 */
} cr_frame;

/* 帧推送回调。data 仅在回调返回前有效;回调内不得长时间阻塞。 */
typedef void(CR_CALL *cr_frame_cb)(void *user, const cr_frame *frame);

/* ---------- 捕获 ---------- */

typedef struct cr_capture cr_capture;

CR_API cr_capture *cr_capture_create(void);
CR_API void cr_capture_destroy(cr_capture *cap);

/* 枚举显示器,返回数量;buf 可传 NULL 仅取数量。Windows 上索引与系统显示器顺序一致。 */
CR_API int cr_capture_list_monitors(cr_capture *cap, int32_t *buf, int buf_count);

/* 枚举可见窗口。两遍调用:先 buf=NULL 取数量,再填充。 */
typedef struct cr_window_info {
    uint64_t hwnd;
    char title[256];
} cr_window_info;
CR_API int cr_capture_list_windows(cr_capture *cap, cr_window_info *buf, int buf_count);

/*
 * 设置捕获目标:
 *   monitor >= 0:整显示器捕获(自带系统光标);
 *   window_hwnd != 0:捕获该窗口所在的显示器并按窗口区域裁剪
 *   (窗口被遮挡时裁剪区域显示遮挡内容,属 OS 限制)。
 * 必须在 start 前调用。
 */
CR_API int cr_capture_set_target(cr_capture *cap, int32_t monitor, uint64_t window_hwnd);

/* 帧率上限(1..60)。v1 不做缩放,输出分辨率 = 捕获源分辨率。start 前调用。 */
CR_API int cr_capture_set_max_fps(cr_capture *cap, int fps);

/* 注册帧回调,start 前调用。 */
CR_API int cr_capture_set_frame_callback(cr_capture *cap, cr_frame_cb cb, void *user);

/* 实际分辨率,start 后有效。 */
CR_API int cr_capture_get_size(cr_capture *cap, int32_t *width, int32_t *height);

/* 启动/停止捕获(内部线程)。start 返回 CR_OK 表示已开始或已在运行。 */
CR_API int cr_capture_start(cr_capture *cap);
CR_API int cr_capture_stop(cr_capture *cap);

/* ---------- 输入注入(Windows VK 层面) ---------- */

typedef struct cr_inject cr_inject;

CR_API cr_inject *cr_inject_create(void);
CR_API void cr_inject_destroy(cr_inject *inj);

/* 键盘:虚拟键码(VK_*)按下/抬起。 */
CR_API int cr_inject_key(cr_inject *inj, uint16_t vk, int down);

/* 鼠标移动:绝对(虚拟屏幕坐标)与相对(像素增量)。 */
CR_API int cr_inject_mouse_move(cr_inject *inj, int32_t x, int32_t y);
CR_API int cr_inject_mouse_move_rel(cr_inject *inj, int32_t dx, int32_t dy);

/* 鼠标按键:0 左 1 中 2 右。 */
CR_API int cr_inject_mouse_button(cr_inject *inj, int button, int down);

/* 滚轮:delta 为 WHEEL_DELTA(120)的倍数,正=向上。 */
CR_API int cr_inject_mouse_wheel(cr_inject *inj, int32_t delta);

/* 文本:UTF-8 字符串,以 Unicode 按键序列注入。 */
CR_API int cr_inject_text(cr_inject *inj, const char *utf8);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CLOUDRENDER_CAPI_CR_API_H */