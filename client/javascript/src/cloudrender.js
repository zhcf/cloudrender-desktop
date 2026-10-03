/**
 * CloudRender JavaScript 客户端 SDK
 *
 * 与 Python/C++ 服务端 SDK 共享同一协议(docs/protocol.md):
 *  - 信令:WebSocket + JSON(connect/connected/offer/answer/ice/ready/stats/error/close)
 *  - 输入:DataChannel "input" 二进制帧,magic "CRIN",小端
 *  - 事件:DataChannel "events" 二进制帧,托管给 onEvent 回调(通过 BinaryMessage 事件透出)
 *
 * 用法:
 *   const client = new CloudRenderClient("ws://host:8080/ws", { video: document.querySelector("video") });
 *   client.on("ready", () => client.keyDown("KeyA"));
 *   client.connect();
 */
export const VERSION = "1.0.0";

export const EV_KIND = {
  CURSOR_POS: 0x01, QUALITY: 0x02, TOAST: 0x03, PONG: 0x04,
};

export const CursorEvent = { KIND: 0x01 };

/* ---------------- FNV-1a/32(与各语言服务端一致)---------------- */
export function fnv1a32(str) {
  const encoder = _fnvEncoder();
  let h = 0x811c9dc5;
  const bytes = encoder.encode(str);
  for (let i = 0; i < bytes.length; i++) {
    h ^= bytes[i];
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}
let _encoder;
function _fnvEncoder() {
  if (!_encoder) _encoder = new TextEncoder();
  return _encoder;
}

/* ---------------- 输入二进制协议(仅打包;解码由服务端完成)---------------- */
const K_KIND = { KEY: 0x01, MOVE: 0x02, BUTTON: 0x03, WHEEL: 0x04, GAMEPAD: 0x05, TOUCH: 0x06, TEXT: 0x07, CUSTOM: 0x7f };

function _eventSize(e) {
  switch (e.kind) {
    case K_KIND.KEY: return 1 + 8 + 1 + 4 + 1 + 1;
    case K_KIND.MOVE: return 1 + 8 + 4 + 4 + 2 + 2;
    case K_KIND.BUTTON: return 1 + 8 + 1 + 1 + 1;
    case K_KIND.WHEEL: return 1 + 8 + 4 + 4 + 1;
    case K_KIND.GAMEPAD: return 1 + 8 + 1 + 1 + 4 + 24;
    case K_KIND.TOUCH: return 1 + 8 + 1 + 1 + 4 + 4 + 4;
    case K_KIND.TEXT: return 1 + 8 + 2 + _fnvEncoder().encode(e.text).length;
    case K_KIND.CUSTOM: return 1 + 8 + 1 + 2 + e.data.length;
    default: throw new Error(`unknown kind ${e.kind}`);
  }
}

function _writeBody(dv, off, e) {
  switch (e.kind) {
    case K_KIND.KEY:
      dv.setUint8(off, e.down ? 1 : 0); off += 1;
      dv.setUint32(off, e.code >>> 0, true); off += 4;
      dv.setUint8(off, e.mods || 0); off += 1;
      dv.setUint8(off, e.repeat ? 1 : 0); off += 1;
      break;
    case K_KIND.MOVE:
      dv.setFloat32(off, e.x, true); off += 4;
      dv.setFloat32(off, e.y, true); off += 4;
      dv.setInt16(off, e.dx || 0, true); off += 2;
      dv.setInt16(off, e.dy || 0, true); off += 2;
      break;
    case K_KIND.BUTTON:
      dv.setUint8(off, e.button); off += 1;
      dv.setUint8(off, e.down ? 1 : 0); off += 1;
      dv.setUint8(off, e.clicks || 1); off += 1;
      break;
    case K_KIND.WHEEL:
      dv.setFloat32(off, e.dx || 0, true); off += 4;
      dv.setFloat32(off, e.dy, true); off += 4;
      dv.setUint8(off, e.ctrl ? 1 : 0); off += 1;
      break;
    case K_KIND.GAMEPAD:
      dv.setUint8(off, e.index || 0); off += 1;
      dv.setUint8(off, e.dpad || 0); off += 1;
      dv.setUint32(off, e.buttons >>> 0, true); off += 4;
      for (let i = 0; i < 6; i++) { dv.setFloat32(off, e.axes[i] || 0, true); off += 4; }
      break;
    case K_KIND.TOUCH:
      dv.setUint8(off, e.id || 0); off += 1;
      dv.setUint8(off, e.phase || 0); off += 1;
      dv.setFloat32(off, e.x, true); off += 4;
      dv.setFloat32(off, e.y, true); off += 4;
      dv.setFloat32(off, e.pressure ?? 1.0, true); off += 4;
      break;
    case K_KIND.TEXT: {
      const bytes = _fnvEncoder().encode(e.text);
      dv.setUint16(off, bytes.length, true); off += 2;
      new Uint8Array(dv.buffer, off, bytes.length).set(bytes); off += bytes.length;
      break;
    }
    case K_KIND.CUSTOM: {
      dv.setUint8(off, e.pluginId || 0); off += 1;
      dv.setUint16(off, e.data.length, true); off += 2;
      new Uint8Array(dv.buffer, off, e.data.length).set(e.data); off += e.data.length;
      break;
    }
  }
  return off;
}

export function encodeInputFrame(events, seq = 0) {
  const n = Math.min(events.length, 255);
  let size = 10;
  for (let i = 0; i < n; i++) size += _eventSize(events[i]);
  const buf = new ArrayBuffer(size);
  const dv = new DataView(buf);
  dv.setUint8(0, 0x43); dv.setUint8(1, 0x52); dv.setUint8(2, 0x49); dv.setUint8(3, 0x4e);
  dv.setUint8(4, 1);                      // ver
  dv.setUint32(5, seq >>> 0, true);       // seq
  dv.setUint8(9, n);                      // count
  let off = 10;
  for (let i = 0; i < n; i++) {
    const e = events[i];
    dv.setUint8(off, e.kind); off += 1;
    dv.setBigUint64(off, BigInt(e.ts || 0), true); off += 8;
    off = _writeBody(dv, off, e);
  }
  return buf;
}

/* ---------------- 客户端 ---------------- */
export class CloudRenderClient {
  constructor(url, options = {}) {
    this.url = url;
    this.video = options.video || null;             // HTMLVideoElement(可选,由 demo 自行处理也能收)
    this.token = options.token || null;
    // 默认不配 STUN:局域网/直连场景 host 候选即可连通,而公网 STUN 不可达时
    // ICE 收集要等到超时(实测拖慢协商约 5s);需要 NAT 穿越时显式传入 iceServers
    this.iceServers = options.iceServers || [];
    this.codecs = options.codecs || ["h264", "vp8"];
    this.width = options.width || 1920;
    this.height = options.height || 1080;
    this.reconnect = options.reconnect !== false;
    this.reconnectDelay = options.reconnectDelay || 1500;
    this.maxReconnectDelay = options.maxReconnectDelay || 10000;
    this.handleInput = options.handleInput !== false;  // SDK 是否自行监听整页输入

    this.pc = null;
    this.ws = null;
    this.dcInput = null;
    this.dcEvents = null;
    this.sessionId = null;
    this.state = "idle";                       // idle|connecting|connected|ready|closed
    this._handlers = new Map();
    this._pending = [];                        // 待 flush 的输入事件
    this._seq = 0;
    this._flushTimer = null;                   // 延迟到 connect() 创建,disconnect() 清理(防废弃实例定时器泄漏)
    this._reconnectAttempts = 0;
    this._closedByUser = false;
    this._stream = null;
    this._statsTimer = null;
    this._remoteResolution = null;
    this._windowListeners = [];
  }

  /* ---------- 事件 ---------- */
  on(name, fn) {
    if (!this._handlers.has(name)) this._handlers.set(name, new Set());
    this._handlers.get(name).add(fn);
    return this;
  }
  off(name, fn) {
    this._handlers.get(name)?.delete(fn);
    return this;
  }
  _emit(name, ...args) {
    this._handlers.get(name)?.forEach((fn) => { try { fn(...args); } catch (e) { console.error(e); } });
  }

  _setState(s) {
    if (this.state !== s) {
      const prev = this.state;
      this.state = s;
      this._emit("statechange", s, prev);
    }
  }

  /* ---------- 连接 ---------- */
  async connect() {
    if (this.ws && (this.ws.readyState === WebSocket.CONNECTING ||
                    this.ws.readyState === WebSocket.OPEN)) {
      return;   // 已在连接/已连接:防同一实例重复发起(重连循环与外部调用并发场景)
    }
    this._closedByUser = false;
    if (!this._flushTimer) this._flushTimer = setInterval(() => this._flushInput(), 16);
    this._setState("connecting");
    const url = this.token ? `${this.url}${this.url.includes("?") ? "&" : "?"}token=${encodeURIComponent(this.token)}` : this.url;
    this.ws = new WebSocket(url);
    this.ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      // 服务端可能在 connected 之前先发 offer,单条消息异常不得破坏后续处理
      this._onSignal(msg).catch((e) => console.error("signal failed", e));
    };
    this.ws.onclose = (ev) => {
      const byUser = this._closedByUser;
      // 服务端因"同客户端重复连接"踢除本连接:已有更新连接接管服务,
      // 本端必须停止重连——否则新旧连接互相踢除会形成无限重连风暴
      const replaced = !byUser && !!ev && ev.code === 1000 &&
                       (ev.reason || "").includes("replaced");
      this._teardown();
      if (replaced) {
        this._closedByUser = true;
        this._setState("closed");
        this._emit("close", ev.reason);
        return;
      }
      if (!byUser && this.reconnect) {
        const delay = Math.min(this.reconnectDelay * Math.pow(2, this._reconnectAttempts++), this.maxReconnectDelay);
        this._setState("connecting");
        setTimeout(() => { if (!this._closedByUser) this.connect(); }, delay);
      } else if (byUser) {
        this._setState("closed");
      }
    };
    this.ws.onerror = () => { /* onclose 统一处理 */ };
    this.ws.onopen = () => {
      this._reconnectAttempts = 0;   // 连接成功即重置退避,避免历史失败次数让重连间隔永久停在最大值
      this._sendSignal({ type: "connect", seq: 1, payload: { client_info: {
        type: "web",
        width: this.width, height: this.height,
        video_codecs: this.codecs, audio: false,
        platform: navigator.platform || "web",
        sdk_version: VERSION,
      } } });
    };
  }

  disconnect() {
    this._closedByUser = true;
    try { this._sendSignal({ type: "disconnect", payload: { reason: "user" } }); } catch { /* noop */ }
    if (this._flushTimer) { clearInterval(this._flushTimer); this._flushTimer = null; }
    this._teardown();
    this._setState("closed");
  }

  _sendSignal(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  async _onSignal(msg) {
    switch (msg.type) {
      case "connected":
        this.sessionId = msg.payload?.session_id;
        if (!this.pc) this._createPeer();  // offer 先到时已创建,避免重复建
        this._setState("connected");
        this._emit("connected", msg.payload);
        break;
      case "offer": {
        if (!this.pc) this._createPeer();  // offer 可能先于 connected 到达
        await this.pc.setRemoteDescription({ type: "offer", sdp: msg.payload.sdp });
        const answer = await this.pc.createAnswer();
        await this.pc.setLocalDescription(answer);
        this._sendSignal({ type: "answer", payload: { sdp: answer.sdp } });
        break;
      }
      case "ice": {
        if (!this.pc) this._createPeer();  // ice 同样可能先于 connected 到达
        const cand = msg.payload.candidate;
        if (!cand) break;
        const str = typeof cand === "string" ? cand : (cand.candidate || cand.sdp || cand.candidateStr);
        if (!str) break;
        try {
          await this.pc.addIceCandidate(str.startsWith("candidate:")
            ? { candidate: str, sdpMid: msg.payload.sdpMid ?? cand.sdpMid, sdpMLineIndex: msg.payload.sdpMLineIndex ?? cand.sdpMLineIndex }
            : JSON.parse(str));
        } catch (e) { console.warn("ice failed", e); }
        break;
      }
      case "ready":
        this._remoteResolution = msg.payload.resolution;
        this._setState("ready");
        this._emit("ready", msg.payload);
        if (!this._statsTimer) this._statsTimer = setInterval(() => this._reportStats(), 2000);
        break;
      case "stats":
        this._emit("stats", msg.payload || {});
        break;
      case "error":
        this._emit("error", msg.payload);
        break;
      case "close":
        this._closedByUser = true;
        this._teardown();
        this._setState("closed");
        this._emit("close", msg.payload?.reason);
        break;
    }
  }

  _createPeer() {
    this.pc = new RTCPeerConnection({ iceServers: this.iceServers.map((u) => ({ urls: u })) });
    this.pc.onicecandidate = (ev) => {
      if (ev.candidate) {
        this._sendSignal({ type: "ice", payload: {
          candidate: {
            candidate: ev.candidate.candidate,
            sdpMid: ev.candidate.sdpMid,
            sdpMLineIndex: ev.candidate.sdpMLineIndex,
          },
        } });
      }
    };
    this.pc.ontrack = (ev) => {
      const stream = ev.streams[0] || new MediaStream([ev.track]);
      this._stream = stream;
      if (this.video) {
        this.video.srcObject = stream;
        this.video.play().catch(() => { /* 自动播放策略 */ });
      }
      this._emit("track", ev.track, stream);
    };
    this.pc.ondatachannel = (ev) => {
      const ch = ev.channel;
      if (ch.label === "input") {
        this.dcInput = ch;
        ch.onopen = () => this._flushInput();
      } else if (ch.label === "events") {
        this.dcEvents = ch;
        ch.onmessage = (m) => this._emit("eventframe", m.data);
      }
    };
  }

  _teardown() {
    try { this.pc?.close(); } catch { /* noop */ }
    this.pc = null;
    this.dcInput = null;
    this.dcEvents = null;
    this._stream = null;
    if (this._statsTimer) { clearInterval(this._statsTimer); this._statsTimer = null; }
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try { ws.close(); } catch { /* noop */ }
    }
  }

  /* ---------- 输入上报 ---------- */
  _queue(e) {
    if (!this.dcInput || this.dcInput.readyState !== "open") return;
    this._pending.push(e);
    if (this._pending.length >= 64) this._flushInput();
  }
  _flushInput() {
    if (!this.dcInput || this.dcInput.readyState !== "open" || this._pending.length === 0) return;
    const events = this._pending.splice(0, 255);
    if (!events.length) return;
    const frame = encodeInputFrame(events, ++this._seq);
    try {
      this.dcInput.send(frame);
    } catch (e) {
      // DataChannel 缓冲满:丢弃本批,避免堆积
      console.warn("input drop", e);
    }
  }

  now() { return Date.now(); }

  keyDown(code, modifiers = 0) { this._queue({ kind: K_KIND.KEY, ts: this.now(), down: 1, code: fnv1a32(code), mods: modifiers }); }
  keyUp(code, modifiers = 0) { this._queue({ kind: K_KIND.KEY, ts: this.now(), down: 0, code: fnv1a32(code), mods: modifiers }); }
  mouseMove(x, y, dx = 0, dy = 0) { this._queue({ kind: K_KIND.MOVE, ts: this.now(), x: x ?? -1, y: y ?? -1, dx, dy }); }
  mouseButton(button, down, clicks = 1) { this._queue({ kind: K_KIND.BUTTON, ts: this.now(), button, down: down ? 1 : 0, clicks }); }
  wheel(dx, dy, ctrl = false) { this._queue({ kind: K_KIND.WHEEL, ts: this.now(), dx: dx || 0, dy: dy || 0, ctrl: ctrl ? 1 : 0 }); }
  touchEvent(id, phase, x, y, pressure = 1.0) { this._queue({ kind: K_KIND.TOUCH, ts: this.now(), id, phase, x, y, pressure }); }
  gamepadState(index, buttons, dpad = 0, axes = []) { this._queue({ kind: K_KIND.GAMEPAD, ts: this.now(), index, dpad, buttons, axes }); }
  sendText(text) { if (text) this._queue({ kind: K_KIND.TEXT, ts: this.now(), text }); }
  custom(pluginId, data) { this._queue({ kind: K_KIND.CUSTOM, ts: this.now(), pluginId, data: data instanceof Uint8Array ? data : new Uint8Array(data) }); }
  requestKeyframe() { this._sendSignal({ type: "video_lost" }); }
  /* 锁定远端桌面:服务端执行 LockWorkStation(等效 Win+L / Ctrl+Alt+Del→锁定);
     锁屏后画面自动切到安全桌面,可直接输入密码解锁 */
  lockScreen() { this._sendSignal({ type: "lock_screen" }); }

  /* ---------- 统计上报 ---------- */
  async _reportStats() {
    if (!this.pc || this.state !== "ready") return;
    try {
      const report = await this.pc.getStats();
      let fps = 0, frames = 0, width = 0, height = 0;
      report.forEach((s) => {
        if (s.type === "inbound-rtp" && s.kind === "video") {
          fps = s.framesPerSecond || 0;
          width = s.frameWidth || 0; height = s.frameHeight || 0;
        }
        if (s.type === "track" && s.kind === "video") frames = s.framesDecoded || 0;
      });
      this._sendSignal({ type: "stats", payload: { clock_ms: Date.now(), decode: { fps: +fps.toFixed(1) } } });
    } catch (e) { /* 静默 */ }
  }

  /* 事件帧解析(events 通道二进制) */
  static parseEventFrame(data) {
    const dv = new DataView(data.buffer || data);
    if (dv.getUint8(0) !== 0x43 || dv.getUint8(1) !== 0x52 || dv.getUint8(2) !== 0x45 || dv.getUint8(3) !== 0x56) return [];
    const n = dv.getUint8(9);
    const out = [];
    let off = 10;
    for (let i = 0; i < n; i++) {
      const kind = dv.getUint8(off); off += 1;
      const ts = Number(dv.getBigUint64(off, true)); off += 8;
      if (kind === EV_KIND.CURSOR_POS) {
        out.push({ kind, ts, x: dv.getFloat32(off, true), y: dv.getFloat32(off + 4, true) }); off += 8;
      } else if (kind === EV_KIND.QUALITY) {
        out.push({ kind, ts, bitrate: dv.getUint32(off, true), fps: dv.getUint8(off + 4) }); off += 5;
      } else if (kind === EV_KIND.TOAST || kind === EV_KIND.PONG) {
        const len = dv.getUint16(off, true); off += 2;
        const bytes = new Uint8Array(dv.buffer, off, len); off += len;
        out.push({ kind, ts, text: new TextDecoder().decode(bytes) });
      } else break;
    }
    return out;
  }
}

export default CloudRenderClient;