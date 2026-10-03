/**
 * CloudRender 协议跨语言契约测试(JS ↔ Python)
 *
 * 金标准字节由 Python 参考实现生成:tools/golden_wire.py(输出 client/javascript/tools/golden.json)。
 * 本测试用相同的一组输入事件(8 种 kind 全覆盖)+ 构造相同的事件通道帧,验证 JS 编码与
 * Python 字节级一致,并反向解析 Python 生成的事件帧、校验 FNV-1a32 基准向量。
 *
 * 运行:node tools/contract_test.mjs (或 npm test)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { fnv1a32, encodeInputFrame, CloudRenderClient } from "../src/cloudrender.js";

const here = dirname(fileURLToPath(import.meta.url));
const goldenPath = join(here, "golden.json");

let golden;
try {
  golden = JSON.parse(readFileSync(goldenPath, "utf8"));
} catch {
  console.error(`缺少金标准文件 ${goldenPath},请先运行:python tools/golden_wire.py`);
  process.exit(1);
}

let failures = 0;
function check(name, actual, expected) {
  const ok = Object.is(actual, expected);
  if (!ok) {
    failures++;
    console.error(`FAIL ${name}\n  actual  : ${actual}\n  expected: ${expected}`);
  }
  return ok;
}
const approx = (a, b) => Math.abs(a - b) < 1e-6;
const hexOf = (buf) => Buffer.from(new Uint8Array(buf)).toString("hex");

/* ---------- 1. FNV-1a32 基准向量 ---------- */
for (const [key, expected] of Object.entries(golden.fnv_vectors)) {
  check(`fnv1a32(${JSON.stringify(key)})`, fnv1a32(key), expected);
}

/* ---------- 2. 输入帧编码(8 种 kind 全覆盖)---------- */
const events = [
  { kind: 0x01, ts: 123, down: 1, code: fnv1a32("KeyA"), mods: 2, repeat: 1 },      // KEY
  { kind: 0x02, ts: 124, x: 0.5, y: 0.25, dx: -3, dy: 2 },                          // MOVE
  { kind: 0x03, ts: 125, button: 1, down: 1, clicks: 2 },                           // BUTTON
  { kind: 0x04, ts: 126, dx: 0.0, dy: 1.0, ctrl: 1 },                               // WHEEL
  { kind: 0x07, ts: 127, text: "hello" },                                           // TEXT(ASCII)
  { kind: 0x05, ts: 128, index: 0, dpad: 3, buttons: 12,                            // GAMEPAD
    axes: [0.1, -0.2, 0, -0.5, 0, 0.5] },
  { kind: 0x7f, ts: 129, pluginId: 9, data: new Uint8Array([0x78, 0x79, 0x7a]) },   // CUSTOM
  { kind: 0x07, ts: 130, text: "你好" },                                            // TEXT(非 ASCII:UTF-8 长度 ≠ UTF-16 长度)
  { kind: 0x06, ts: 131, id: 2, phase: 1, x: 0.25, y: 0.5, pressure: 0.9 },          // TOUCH
];
const frame = encodeInputFrame(events, 42);
check("input frame bytes", hexOf(frame), golden.input_hex);

/* ---------- 3. 事件帧反向解析(Python 编码 → JS 解析)---------- */
const evBuf = new Uint8Array(Buffer.from(golden.events_hex, "hex"));
const parsed = CloudRenderClient.parseEventFrame(evBuf.buffer);
check("events count", parsed.length, 3);

const [cursor, quality, toast] = parsed;
if (cursor) {
  check("cursor kind", cursor.kind, 0x01);
  check("cursor ts", cursor.ts, 200);
  check("cursor x", approx(cursor.x, 0.75), true);
  check("cursor y", approx(cursor.y, -0.125), true);
}
if (quality) {
  check("quality kind", quality.kind, 0x02);
  check("quality ts", quality.ts, 201);
  check("quality bitrate", quality.bitrate, 4500);
  check("quality fps", quality.fps, 30);
}
if (toast) {
  check("toast kind", toast.kind, 0x03);
  check("toast ts", toast.ts, 202);
  check("toast text", toast.text, "你好,云桌面");
}

/* ---------- 4. 非法帧必须被拒绝 ---------- */
check("bad magic rejected", CloudRenderClient.parseEventFrame(new Uint8Array([1, 2, 3, 4, 0, 0, 0, 0, 0, 0]).buffer).length, 0);

if (failures === 0) {
  console.log(`CONTRACT_OK input=${frame.byteLength}B events=${evBuf.length}B`);
} else {
  console.error(`CONTRACT_FAIL ${failures} checks failed`);
  process.exit(1);
}