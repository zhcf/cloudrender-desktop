/**
 * CloudRender 会话核心 - C ABI(cloudrender_session.dll)
 *
 * 供 Python(ctypes)/任意语言加载:封装 libwebrtc 服务端会话
 * (offer/answer/ICE + 视频轨 + input/events DataChannel)并直连 nativecore
 * 抓屏与输入注入。信令事件不跨线程回调外部代码:内部排队,由轮询接口拉取。
 *
 * 线程模型:除特别说明外可在任意线程调用;cr_session_poll_signal 建议由
 * 单一线程轮询(如 Python 信令壳的 asyncio 循环线程)。
 *
 * 典型流程:
 *   s = cr_session_create(cfg)
 *   sdp = cr_session_create_offer(s)        → 发给浏览器
 *   loop: 对端信令 → cr_session_handle_signal(s, ...)
 *         cr_session_poll_signal(s)         → 发 ICE/closed 给浏览器
 *   cr_session_close(s); cr_session_destroy(s)
 */
#ifndef CLOUDRENDER_CAPI_CR_SESSION_API_H
#define CLOUDRENDER_CAPI_CR_SESSION_API_H

#include <stdint.h>

#ifdef _WIN32
#ifdef CR_SESSION_BUILD
#define CR_SESSION_API __declspec(dllexport)
#else
#define CR_SESSION_API __declspec(dllimport)
#endif
#define CR_SESSION_CALL __cdecl
#else
#define CR_SESSION_API __attribute__((visibility("default")))
#define CR_SESSION_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* 返回码 */
enum {
    CR_SESSION_OK = 0,
    CR_SESSION_E_ARG = 1,   /* 参数非法 */
    CR_SESSION_E_STATE = 2, /* 状态错误(如未就绪) */
    CR_SESSION_E_FAIL = 3   /* 操作失败 */
};

typedef struct cr_session cr_session;

/*
 * 创建会话(libwebrtc 初始化 + nativecore 抓屏/注入 + PeerConnection 就绪)。
 *
 * config_json 可为 NULL/空串(全部默认)。支持字段(均为可选):
 *   "monitor":     int      显示器索引(默认 -1 = 主显示器)
 *   "window_hwnd": uint64   捕获窗口句柄(非 0 时按窗口所在显示器+区域裁剪)
 *   "fps":         int      帧率上限 1..60(默认 30)
 *   "ice_servers": ["stun:...", ...]  传空数组 = 不使用 STUN;缺省 = 内置默认
 *
 * 成功后会立即启动抓屏(帧泵在 ICE/DTLS 连接建立后自动推送)。
 * 失败(libwebrtc 初始化/抓屏启动失败)返回 NULL。
 */
CR_SESSION_API cr_session* CR_SESSION_CALL cr_session_create(const char* config_json);

/* 销毁会话(内部先 close,幂等)。NULL 安全。 */
CR_SESSION_API void CR_SESSION_CALL cr_session_destroy(cr_session* s);

/*
 * 创建 offer 并设置本地描述(阻塞,最长约 5s)。成功返回 SDP 文本。
 * 返回值须用 cr_session_free 释放;失败返回 NULL。
 * 注意:offer 只经此返回值交付,不会出现在 cr_session_poll_signal 中。
 */
CR_SESSION_API char* CR_SESSION_CALL cr_session_create_offer(cr_session* s);

/*
 * 处理客户端信令:
 *   type ∈ "answer" / "ice" / "disconnect"
 *   payload_json 为 payload 对象 JSON(如 {"sdp":"..."} / {"candidate":{...}})
 * 返回 CR_SESSION_OK 或错误码。
 */
CR_SESSION_API int CR_SESSION_CALL cr_session_handle_signal(cr_session* s,
                                                            const char* type,
                                                            const char* payload_json);

/*
 * 拉取一条待发送信令(非阻塞)。返回 JSON 文本(须用 cr_session_free 释放):
 *   {"type":"ice","payload":{"candidate":{...}}}      本地 ICE 候选
 *   {"type":"closed","payload":{"reason":"..."}}      会话结束
 * 无待发数据返回 NULL。
 */
CR_SESSION_API char* CR_SESSION_CALL cr_session_poll_signal(cr_session* s);

/* ICE/DTLS 已连接(可出画)。 */
CR_SESSION_API int CR_SESSION_CALL cr_session_connected(cr_session* s);

/* 当前捕获分辨率(就绪返回 CR_SESSION_OK 并写出;未就绪返回状态错误)。 */
CR_SESSION_API int CR_SESSION_CALL cr_session_get_source_size(cr_session* s,
                                                             int* width, int* height);

/*
 * 查询发送侧累计统计(outbound-rtp 的 bytesSent + 帧泵累计推送帧数)。
 * 返回 JSON 文本(须用 cr_session_free 释放):
 *   {"bytes_sent":12345,"frames_pushed":67}
 * 未连接/查询超时返回 NULL。上层按两次采样的差分计算 fps/码率。
 */
CR_SESSION_API char* CR_SESSION_CALL cr_session_get_stats(cr_session* s);

/*
 * 设置视频目标码率(bps,min=max 固定;带宽估计起步偏低:不设会前十几秒
 * 画面又糊又卡)。运行期任意线程可调,幂等;建议 connected 后周期重申。
 * bitrate_bps<=0 返回 CR_SESSION_E_ARG;未就绪/失败返回错误码。
 */
CR_SESSION_API int CR_SESSION_CALL cr_session_set_bitrate(cr_session* s,
                                                          int bitrate_bps);

/*
 * 安全桌面(锁屏)模式:on=1 后帧泵改推 cr_session_push_secure_frame 注入的帧
 * (普通权限进程锁屏期间无法抓屏),input 通道收到的输入帧不再本地注入,改为
 * 经 cr_session_poll_signal 的 {"type":"secure_input","payload":{"data":"<hex>"}}
 * 旁路给上层(转发 SYSTEM worker 注入安全桌面);on=0 恢复本机抓屏/注入。
 * 任意线程可调,幂等。
 */
CR_SESSION_API int CR_SESSION_CALL cr_session_set_secure_mode(cr_session* s,
                                                              int on);

/*
 * 注入一帧安全桌面图像(BGRA32,stride 字节/行,0=紧凑 width*4;数据被立即
 * 拷贝,调用后可释放)。仅在安全桌面模式下生效;上层按帧率重复调用,无新帧
 * 时帧泵重复最后一帧。bgra 为空/宽高非正/stride 小于 width*4 返回 E_ARG。
 */
CR_SESSION_API int CR_SESSION_CALL cr_session_push_secure_frame(cr_session* s,
                                                               const uint8_t* bgra,
                                                               int width, int height,
                                                               int stride,
                                                               uint64_t pts_us);

/*
 * 经 events 数据通道发送一条 toast 提示(UTF-8)。未连接/通道未就绪
 * 返回状态错误(上层可重试)。
 */
CR_SESSION_API int CR_SESSION_CALL cr_session_send_toast(cr_session* s,
                                                         const char* utf8_text);

/* 关闭会话(停止帧泵/PeerConnection/抓屏;幂等)。 */
CR_SESSION_API void CR_SESSION_CALL cr_session_close(cr_session* s);

/* 释放本接口返回的堆字符串。 */
CR_SESSION_API void CR_SESSION_CALL cr_session_free(char* p);

#ifdef __cplusplus
}
#endif

#endif /* CLOUDRENDER_CAPI_CR_SESSION_API_H */