"""生成协议金标准字节(供 JS/C++ 契约测试比对)。

本脚本加载 cloudrender.protocol,构造一组覆盖全部 8 种 kind 的 9 个输入事件 +
3 种事件通道消息的帧,输出稳定 hex。注意:所有 ts 均为固定常量,与 Date.now() 无关。

输出两个金标准文件:
  - client/javascript/tools/golden.json(JS 契约测试读取)
  - server/cpp/sdk/tests/golden.txt(C++ cr_wire 契约测试读取)
"""
import importlib.util
import json
import os
import sys

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(here, "server", "python"))
sys.modules.pop("cloudrender", None)

spec = importlib.util.spec_from_file_location(
    "cloudrender.protocol",
    os.path.join(here, "server", "python", "cloudrender", "protocol.py"))
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)

# ---- 输入帧:seq=42,9 个事件(8 种 kind 全覆盖,含非 ASCII 文本) ----
events = [
    p.InputEvent.key(1, "KeyA", 2, 1, 123),
    p.InputEvent.mouse_move(0.5, 0.25, -3, 2, 124),
    p.InputEvent.mouse_button(1, 1, 2, 125),
    p.InputEvent.wheel(0.0, 1.0, 1, 126),
    p.InputEvent.text("hello", 127),
    p.InputEvent.gamepad(0, 12, (0.1, -0.2, 0.0, -0.5, 0.0, 0.5), 3, 128),
    p.InputEvent.custom(9, b"xyz", 129),
    p.InputEvent.text("你好", 130),  # 2 个 UTF-16 码元 / 6 个 UTF-8 字节
    p.InputEvent(p.KIND_TOUCH, 131, (2, 1, 0.25, 0.5, 0.9)),  # TOUCH = id/phase/x/y/pressure
]
input_blob = p.encode_input_frame(events, seq=42)

# Python 自校验往返
seq, out = p.decode_input_frame(input_blob)
assert seq == 42 and len(out) == len(events)

# ---- 事件通道帧:seq=7 ----
event_blob = p.encode_event_frame([
    p.ev_cursor_pos(0.75, -0.125, 200),
    p.ev_quality(4500, 30, 201),
    p.ev_toast("你好,云桌面", 202),
], seq=7)

# ---- FNV 基准向量 ----
fnv_vectors = {
    "KeyA": p.fnv1a32("KeyA"),
    "Enter": p.fnv1a32("Enter"),
    "": p.fnv1a32(""),
    "你好": p.fnv1a32("你好"),
}

print("INPUT_HEX=" + input_blob.hex())
print("EVENTS_HEX=" + event_blob.hex())
print("FNV_VECTORS=" + json.dumps(fnv_vectors))

# 落盘供 JS/C++ 契约测试读取(避免人工转录 hex)
out_path = os.path.join(here, "client", "javascript", "tools", "golden.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({
        "input_hex": input_blob.hex(),
        "events_hex": event_blob.hex(),
        "fnv_vectors": fnv_vectors,
    }, f, ensure_ascii=False, indent=2)
print("GOLDEN_WRITTEN=" + out_path)

# C++ 契约测试用三行文本格式(测试只依赖标准库,无 JSON 解析):
#   INPUT_HEX=<hex>
#   EVENTS_HEX=<hex>
#   FNV_VECTORS=name=value ...(空 key 呈现为 "=value")
txt_path = os.path.join(here, "server", "cpp", "sdk", "tests", "golden.txt")
# newline="\n" 固定 LF:CRLF 行尾会让读取端的 hex 尾部混入 '\r'
with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
    f.write("INPUT_HEX=" + input_blob.hex() + "\n")
    f.write("EVENTS_HEX=" + event_blob.hex() + "\n")
    f.write("FNV_VECTORS=" + " ".join(f"{k}={v}" for k, v in fnv_vectors.items()) + "\n")
print("GOLDEN_TXT_WRITTEN=" + txt_path)