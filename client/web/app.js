/* CloudRender 云桌面 Demo 前端逻辑:输入采集 → SDK → 服务端注入 */
/* 页面由服务端内置托管(与 /ws 同端口):信令地址默认取页面同源,免手工填写;
   ?ws= 显式覆盖(调试/远程场景),?token= 透传服务端校验 */
/* SDK 引用带版本(与 index.html 的 app.js?v= 同步升级):无版本的模块 URL
   会被浏览器按启发式规则直接使用旧缓存(不发请求),导致页面脚本与 SDK 版本错位 */
import CloudRenderClient from "../javascript/src/cloudrender.js?v=14";

/* 从 script src 的 ?v= 参数取脚本版本,连接日志中展示(用于确认浏览器加载的代码版本) */
const UI_VERSION = new URL(import.meta.url).searchParams.get("v") || "?";

const $ = (id) => document.getElementById(id);
const video = $("screen");
const logLine = (msg) => {
  const el = $("log");
  el.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.style.opacity = 1;
  setTimeout(() => (el.style.opacity = 0.35), 4000);
};

let client = null;
let connected = false;
let capturingInput = false;      // 是否把本页输入发往远端
let pointerLocked = false;
let lastGamepad = {};

/* ---------- 连接 ---------- */
/* 信令地址:与页面同源(服务端内置托管,页面与 ws 同端口);?ws= 覆盖调试/远程场景 */
const _qs = new URLSearchParams(location.search);
function signalUrl() {
  const override = _qs.get("ws");
  if (override) return override;
  // file:// 直开(无宿主服务端):回退本机默认端口
  if (location.protocol === "file:") return "ws://127.0.0.1:8080/ws";
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

$("btn-connect").addEventListener("click", () => {
  if (connected) {
    client.disconnect();
    return;
  }
  doConnect();
});

/* 锁屏按钮:发 lock_screen 信令,服务端 LockWorkStation 锁定远端桌面(等效 Win+L);
   锁屏后画面自动切到安全桌面,可在浏览器中直接输入密码解锁 */
$("btn-lock").addEventListener("click", () => {
  if (!client || !connected) { logLine("尚未连接:锁屏按钮在会话建立后可用"); return; }
  if (typeof client.lockScreen !== "function") {   // 脚本版本错位(旧 SDK 被浏览器缓存)
    logLine("前端脚本版本过期:请按 Ctrl+F5 强制刷新后重试");
    return;
  }
  client.lockScreen();
  logLine("已发送锁屏指令");
});

/* 全屏按钮:浏览器级全屏(本地,不影响发往远端的按键)。捕获状态下 F11 会被
   转发到远端,本地全屏用此按钮(或先将焦点放进工具栏再按 F11);Esc 退出 */
const isFullscreen = () => !!document.fullscreenElement;
$("btn-fullscreen").addEventListener("click", () => {
  if (isFullscreen()) {
    document.exitFullscreen();
  } else {
    const p = document.documentElement.requestFullscreen?.();
    p?.catch(() => logLine("浏览器拒绝全屏请求:请再试一次"));
  }
  focusKbd();   // 焦点交还捕获区:全屏后键盘继续直达远端
});
document.addEventListener("fullscreenchange", () => {
  $("btn-fullscreen").textContent = isFullscreen() ? "退出全屏" : "全屏";
});

function doConnect() {
  const url = signalUrl();
  const token = (_qs.get("token") || "").trim();
  // 单实例治理:重复发起连接前先销毁旧实例——旧实例的自动重连循环
  // 若与新实例并存,会与服务端"同客户端踢旧"治理互相踢除、无限重连
  if (client) {
    try { client.disconnect(); } catch (e) { /* noop */ }
    client = null;
  }
  client = new CloudRenderClient(url, { video, token: token || null });
  bindClient(client);
  client.connect();
}

function bindClient(c) {
  c.on("statechange", (s) => {
    const dot = $("state-dot");
    dot.className = "dot " + s;
    const texts = { idle: "未连接", connecting: "连接中…", connected: "协商中…", ready: "已连接", closed: "已断开" };
    $("state-text").textContent = texts[s] || s;
    if (s === "connected" || s === "ready") {
      $("btn-connect").textContent = "Disconnect";
      connected = true;
      $("overlay").classList.add("hidden");
    } else if (s === "closed" || s === "idle") {
      $("btn-connect").textContent = "Connect";
      connected = false;
      capturingInput = false;
      exitPointerLock();
      $("overlay").classList.remove("hidden");
    }
  });
  c.on("ready", (payload) => {
    if (payload.resolution) {
      $("stat-res").textContent = `${payload.resolution.width}x${payload.resolution.height}`;
    }
    logLine(`会话建立 source=${payload.source} codec=${payload.codec} ui=v${UI_VERSION}`);
    enableCapture();
  });
  c.on("stats", (s) => {
    if (s.video) {
      $("stat-fps").textContent = `${s.video.fps_sent} fps`;
      $("stat-br").textContent = `${s.video.bitrate_kbps} kbps`;
    }
  });
  c.on("error", (e) => logLine(`服务端错误: ${e.message} (code=${e.code})`));
  c.on("close", (r) => logLine(`会话关闭: ${r || "unknown"}`));
  c.on("track", () => { video.hidden = false; });
  c.on("eventframe", (bytes) => {
    for (const ev of CloudRenderClient.parseEventFrame(bytes)) {
      if (ev.kind === 0x03) logLine(`远端提示: ${ev.text}`);
    }
  });
}

/* ---------- 输入采集 ---------- */
function enableCapture() {
  capturingInput = true;
}

/* ---------- 键盘:点击画面后直接在浏览器上输入 ----------
   隐藏捕获区(kbd-capture)承载焦点与软键盘;字符/删除/回车/中文
   统一经 beforeinput/composition 事件发往远端,捕获区自身不存内容 */
const modsOf = (e) =>
  (e.shiftKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.altKey ? 4 : 0) | (e.metaKey ? 8 : 0);
const KEY_SPECIAL = ["Backspace", "Enter", "Delete"];

/* 硬键盘 keydown/keyup:功能键与组合键直接发远端;捕获区内可打印字符/删除/回车
   交给 beforeinput 统一发送(避免双发)。关键:输入法处理中的按键与任何可打印
   字符的物理键绝不外发——一旦泄漏到远端,会触发远端输入法二次组合,
   导致本地与远端候选词框同时出现 */
function isImeKey(e) {
  return e.isComposing || e.keyCode === 229 ||
         e.key === "Process" || e.key === "Dead" || e.key === "Unidentified";
}
const isPrintable = (e) =>
  e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey;

function skipGlobalKey(e) {
  if (isImeKey(e)) return true;                       // 输入法组合/占位键:永不外发
  if (pointerLocked) return false;                    // 鼠标锁定(游戏相对模式):物理键全发
  if (e.target.closest && e.target.closest("#toolbar")) return true; // 工具栏:留给本地
  if (e.target !== $("kbd-capture")) {
    return isPrintable(e);   // 捕获区外:可打印字符不发(防输入法字母泄漏),功能键照发
  }
  if (KEY_SPECIAL.includes(e.key)) return true;       // 交给 beforeinput 统一发送
  return isPrintable(e);
}
document.addEventListener("keydown", (e) => {
  if (!capturingInput || !client || skipGlobalKey(e)) return;
  e.preventDefault();
  client.keyDown(e.code, modsOf(e));
});
document.addEventListener("keyup", (e) => {
  if (!capturingInput || !client || skipGlobalKey(e)) return;
  e.preventDefault();
  client.keyUp(e.code, modsOf(e));
});

/* 捕获区内输入 → 远端:删除/回车发按键帧,文本发 TEXT 帧(中文经 IME 提交) */
let kbdComposing = false;
let kbdLastCommit = { text: "", ts: 0 };  // 最近一次 IME 提交(识别平台补发的 insertText)
const syncKey = (code) => { client.keyDown(code, 0); client.keyUp(code, 0); };

$("kbd-capture").addEventListener("compositionstart", () => { kbdComposing = true; });
$("kbd-capture").addEventListener("compositionend", (e) => {
  kbdComposing = false;
  $("kbd-capture").value = "";   // 组合已结束:清空组合期间暂存文本(程序性赋值不触发 input)
  if (client && capturingInput && e.data) {
    client.sendText(e.data);
    kbdLastCommit = { text: e.data, ts: performance.now() };
  }
});
$("kbd-capture").addEventListener("beforeinput", (e) => {
  if (!capturingInput || !client) return;
  // 输入法组合中:完全不干预(不 preventDefault、不外发),交还给输入法自然处理。
  // 若在组合中拦截/转发编辑事件,会导致 QQ 拼音等输入法组合状态被打断:
  // 残留的单个 "i" 再按数字键会误触发输入法 i 模式小工具,且退格会误删远端内容
  if (kbdComposing || e.isComposing || e.inputType === "insertCompositionText") return;
  switch (e.inputType) {
    case "insertLineBreak": e.preventDefault(); syncKey("Enter"); break;
    case "deleteContentBackward": e.preventDefault(); syncKey("Backspace"); break;
    case "deleteWordBackward": e.preventDefault(); syncKey("Backspace"); break;
    case "deleteContentForward": e.preventDefault(); syncKey("Delete"); break;
    case "insertText":
      if (e.data) {
        e.preventDefault();
        // 部分平台提交后会补发一次(或多次)与提交内容重复的 insertText:
        // 短时间窗内内容一致视为回声不发送,避免远端出现双份文本;
        // 无补发时也不会残留标志误吞后续正常字符
        const echo = !kbdComposing && e.data === kbdLastCommit.text &&
                     performance.now() - kbdLastCommit.ts < 120;
        if (!echo) client.sendText(e.data);
      }
      break;
  }
});
$("kbd-capture").addEventListener("input", (e) => {
  // 组合中不清空(会打断输入法组合);组合结束或普通输入后才清空
  if (!kbdComposing) e.target.value = "";
});

const focusKbd = () => { if (!pointerLocked) $("kbd-capture").focus(); };
window.addEventListener("wheel", (e) => {
  if (!capturingInput || !client) return;
  e.preventDefault();
  client.wheel(e.deltaX, e.deltaY, e.ctrlKey);
}, { passive: false });

/* 鼠标:有指针锁定时用相对移动;否则用视频画布归一化坐标 */
document.addEventListener("pointerlockchange", () => {
  pointerLocked = document.pointerLockElement === video;
});

/* video 元素与画面宽高比不同时 object-fit 会留黑边(letterbox),
   归一化坐标必须基于内容实际显示区域,否则点行首/边缘会错位 */
function videoContentRect() {
  const r = video.getBoundingClientRect();
  if (!video.videoWidth || !video.videoHeight) return r;
  const scale = Math.min(r.width / video.videoWidth, r.height / video.videoHeight);
  const cw = video.videoWidth * scale;
  const ch = video.videoHeight * scale;
  return { left: r.left + (r.width - cw) / 2, top: r.top + (r.height - ch) / 2,
           width: cw, height: ch };
}

document.addEventListener("mousemove", (e) => {
  if (!capturingInput || !client) return;
  if (pointerLocked) {
    client.mouseMove(-1, -1, e.movementX || 0, e.movementY || 0);
  } else if (video.videoWidth) {
    const r = videoContentRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    client.mouseMove(Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y)), 0, 0);
  }
});
/* 鼠标按键值:0=左 1=中 2=右(与服务端注入映射一致);其他按键(如 X1/X2)回退为左键 */
const normButton = (b) => (b >= 0 && b <= 2 ? b : 0);
document.addEventListener("mousedown", (e) => {
  if (!capturingInput || !client || e.target.closest("#toolbar")) return;
  e.preventDefault();
  client.mouseButton(normButton(e.button), true);
  const usesLock = e.target === video;
  if (usesLock && $("opt-pointerlock").checked && !pointerLocked) {
    video.requestPointerLock?.();
  } else if (usesLock) {
    focusKbd();   // 点击画面后键盘输入即可直达远端
  }
});
document.addEventListener("mouseup", (e) => {
  if (!capturingInput || !client) return;
  client.mouseButton(normButton(e.button), false);
});
/* 右键:阻止浏览器原生右键菜单,使右键事件转发到远端(工具栏除外,保留本地右击菜单) */
document.addEventListener("contextmenu", (e) => {
  if (!capturingInput || !client || e.target.closest("#toolbar")) return;
  e.preventDefault();
});

/* 触摸:直接映射到远端(单指=左键,双指=滚轮,三指=右键,由服务端转换) */
const touchPos = (t) => {
  const r = videoContentRect();
  return [Math.min(1, Math.max(0, (t.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (t.clientY - r.top) / r.height))];
};
video.addEventListener("touchstart", (e) => {
  if (!capturingInput || !client) return;
  e.preventDefault();
  for (const t of e.changedTouches) {
    const [x, y] = touchPos(t);
    client.touchEvent(t.identifier, 0, x, y);
  }
  focusKbd();
}, { passive: false });
video.addEventListener("touchmove", (e) => {
  if (!capturingInput || !client) return;
  e.preventDefault();
  for (const t of e.changedTouches) {
    const [x, y] = touchPos(t);
    client.touchEvent(t.identifier, 1, x, y);
  }
}, { passive: false });
video.addEventListener("touchend", (e) => {
  if (!capturingInput || !client) return;
  e.preventDefault();
  for (const t of e.changedTouches) client.touchEvent(t.identifier, 2, 0, 0);
}, { passive: false });
video.addEventListener("touchcancel", (e) => {
  if (!capturingInput || !client) return;
  for (const t of e.changedTouches) client.touchEvent(t.identifier, 2, 0, 0);
});

/* 手柄轮询 */
function padButtons(pad) {
  let bits = 0;
  for (let i = 0; i < Math.min(10, pad.buttons.length); i++) bits |= (pad.buttons[i].pressed ? 1 : 0) << i;
  return bits >>> 0;
}
function padDpad(pad) {
  let d = 0;
  d |= (pad.buttons[12]?.pressed ? 1 : 0);
  d |= (pad.buttons[13]?.pressed ? 2 : 0);
  d |= (pad.buttons[14]?.pressed ? 4 : 0);
  d |= (pad.buttons[15]?.pressed ? 8 : 0);
  return d;
}
setInterval(() => {
  if (!capturingInput || !client) return;
  for (const pad of navigator.getGamepads?.() || []) {
    if (!pad) continue;
    const axes = [pad.axes[0] || 0, pad.axes[1] || 0, pad.axes[2] || 0, pad.axes[3] || 0,
                  pad.buttons[6]?.value || 0, pad.buttons[7]?.value || 0];
    const key = [pad.index, axes.join(",")].join("|");
    if (lastGamepad[pad.index] === key) continue;
    lastGamepad[pad.index] = key;
    client.gamepadState(pad.index, padButtons(pad), padDpad(pad), axes);
  }
}, 32);

function exitPointerLock() {
  if (document.pointerLockElement) document.exitPointerLock?.();
}

/* 页面关闭前断开 */
window.addEventListener("beforeunload", () => client?.disconnect());