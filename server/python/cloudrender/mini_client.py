"""CloudRender Python 客户端脚本:验证信令/输入协议双端一致。

无 GUI:连接后消费解码帧统计 fps;命令行可注入输入。用法:

    python -m cloudrender.mini_client ws://127.0.0.1:8080/ws
    命令: move <x> <y> | click <button> | key <code> | text <str> | quit
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# 添加 server/python 到 sys.path:直接运行或 -m 运行都能 import cloudrender
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiortc import RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

try:
    import aiohttp
except ImportError:
    print("需要 aiohttp:pip install aiohttp")
    raise

from cloudrender import protocol


class Client:
    def __init__(self, url: str):
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        self.url = url
        self.ws = None
        # 局域网/直连场景无需 STUN:host 候选即可连通,且公共 STUN 不可达时
        # ICE 收集要等到超时拖慢协商(需要 NAT 穿越时自行配置 iceServers)
        self.pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=[]))
        self.dc = None
        self.seq = 0
        self.fps = 0.0
        self._frames = 0
        self._fps_t0 = time.time()
        self._ready = False

    async def connect(self):
        session = aiohttp.ClientSession()
        self.ws = await session.ws_connect(self.url)
        # 与服务端数据通道对接(浏览器由服务端 offer;这里作为 answer 端)
        from aiortc import RTCDataChannel
        # aiortc 不暴露原始 SCTP 通道列表;用 on("datachannel") 回调
        self.pc.on("datachannel", self._on_datachannel)

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                asyncio.ensure_future(self._consume(track))

        await self.ws.send_json(protocol.make_message(
            "connect",
            client_info={"type": "other", "width": 1280, "height": 720,
                         "video_codecs": ["h264", "vp8"], "audio": False,
                         "platform": "python-mini-client", "sdk_version": "1.0.0"}))
        # 消息循环
        async for msg in self.ws:
            data = json.loads(msg.data)
            t = data.get("type")
            if t == "offer":
                offer = RTCSessionDescription(sdp=data["payload"]["sdp"], type="offer")
                await self.pc.setRemoteDescription(offer)
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                await self.ws.send_json(protocol.make_message(
                    "answer", sdp=self.pc.localDescription.sdp))
            elif t == "ice":
                payload = data.get("payload") or {}
                c = payload.get("candidate")
                # libwebrtc 服务端为标准 trickle ICE:候选经信令单独下发,
                # aiortc 要求候选对象带 sdpMid/sdpMLineIndex 才接受
                mid = payload.get("sdpMid")
                mline = payload.get("sdpMLineIndex")
                if isinstance(c, dict):
                    mid = c.get("sdpMid", mid)
                    mline = c.get("sdpMLineIndex", mline)
                    c = c.get("candidate") or c.get("sdp") or c.get("candidateStr")
                if c:
                    cand = candidate_from_sdp(c.split(":", 1)[1] if c.startswith("candidate:") else c)
                    cand.sdpMid = mid
                    cand.sdpMLineIndex = mline
                    await self.pc.addIceCandidate(cand)
            elif t == "ready":
                self._ready = True
                print(f"[{time.strftime('%H:%M:%S')}] 视频已就绪 "
                      f"{data['payload'].get('resolution')}")
            elif t == "stats":
                s = data.get("payload") or {}
                print(f"\r[stats] {s.get('video', {})}", end="", flush=True)
            elif t == "error":
                print("!! 服务端错误:", data)
                break
            elif t == "close":
                print("服务端关闭会话")
                break

    def _on_datachannel(self, channel):
        if channel.label == "events":
            channel.on("message", lambda m: self._on_event(m))
        # input 通道由客户端发,复用 label 匹配
        if channel.label == "input":
            self.dc = channel

    def _on_event(self, msg):
        try:
            kind = msg[0]
        except Exception:
            return
        if kind == protocol.EVT_TOAST:
            print("\n[toast]", msg[9:].decode("utf-8", "replace"))

    async def _consume(self, track):
        while True:
            try:
                frame = await track.recv()
            except Exception:
                break
            self._frames += 1
            now = time.time()
            if now - self._fps_t0 >= 5:
                self.fps = self._frames / (now - self._fps_t0)
                self._frames = 0
                self._fps_t0 = now
                print(f"\n[decode] {self.fps:.1f} fps")

    async def send_input(self, events) -> None:
        if not self.dc or self.dc.readyState != "open":
            print("输入通道未就绪")
            return
        self.seq += 1
        self.dc.send(protocol.encode_input_frame(events, seq=self.seq))

    async def console_loop(self) -> None:
        print("已连接。命令: move <x> <y> | click <button> | key <code> | text <s> | quit")
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, input)
            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            ts = int(time.time() * 1000)
            if cmd == "quit":
                break
            elif cmd == "move":
                x, y = arg.split()
                await self.send_input(
                    [protocol.InputEvent.mouse_move(float(x) / 1920, float(y) / 1080, 0, 0, ts)])
            elif cmd == "click":
                btn = int(arg or 0)
                await self.send_input([
                    protocol.InputEvent.mouse_button(btn, 1, 1, ts),
                    protocol.InputEvent.mouse_button(btn, 0, 1, ts + 50),
                ])
            elif cmd == "key":
                await self.send_input([
                    protocol.InputEvent.key(1, arg, 0, 0, ts),
                    protocol.InputEvent.key(0, arg, 0, 0, ts + 50),
                ])
            elif cmd == "text":
                await self.send_input([protocol.InputEvent.text(arg, ts)])
            else:
                print("未知命令:", cmd)


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8080/ws"
    client = Client(url)
    try:
        task = asyncio.create_task(client.console_loop())
        await client.connect()
    finally:
        if client.pc:
            await client.pc.close()
        if client.ws:
            await client.ws.close()


if __name__ == "__main__":
    asyncio.run(main())