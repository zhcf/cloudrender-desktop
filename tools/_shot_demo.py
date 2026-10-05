# -*- coding: utf-8 -*-
"""headless Edge + CDP:打开云桌面 demo,连接并截图(README 配图)
可选参数:覆盖视频区域的标准桌面图路径(默认 docs/images/win11-desktop.jpg,传 none 禁用)"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

import aiohttp
from aiohttp import web

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9223
URL = "http://localhost:8080/"
ROOT = r"c:\zhcf\pythonproject\cloudrender"
OUT = os.path.join(ROOT, "docs", "images", "usage-demo.png")
# 标准 Win11 桌面素材:截图时覆盖视频画面区域(避免截到真实桌面);参数传 none 可禁用
OVERLAY_IMG = os.path.join(ROOT, "docs", "images", "win11-desktop.jpg")
OVERLAY_PORT = 8123


async def wait_endpoint(sess, tries=60):
    for _ in range(tries):
        try:
            async with sess.get("http://127.0.0.1:%d/json/version" % PORT) as r:
                await r.json()
                return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send_json({"id": mid, "method": method, "params": params})
        while True:
            msg = await self.ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError("%s -> %s" % (method, data["error"]))
                return data.get("result", {})

    async def eval(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")


# 用标准 Win11 桌面图覆盖 #screen 视频元素整个区域,避免截到真实桌面(并保留日志行在最顶层)
OVERLAY_JS = (
    "(function(){"
    "var v=document.getElementById('screen');"
    "var r=v.getBoundingClientRect();"
    "var img=new Image();"
    "img.style.cssText='position:fixed;pointer-events:none;z-index:2147483000;left:'"
    "+r.left+'px;top:'+r.top+'px;width:'+r.width+'px;height:'+r.height+'px;';"
    "img.src='http://127.0.0.1:%d/desktop.jpg?t='+Date.now();"
    "window.__ovl=img;"
    "document.body.appendChild(img);"
    "var lg=document.getElementById('log');"
    "if(lg){lg.style.zIndex='2147483600';}"
    "return true;"
    "})()"
) % OVERLAY_PORT


async def start_overlay_server(img_path):
    async def handler(request):
        return web.FileResponse(img_path)

    app = web.Application()
    app.router.add_get("/desktop.jpg", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", OVERLAY_PORT).start()
    return runner


async def main():
    udd = tempfile.mkdtemp(prefix="crdemo_shot_")
    proc = subprocess.Popen(
        [
            EXE, "--headless=new",
            "--remote-debugging-port=%d" % PORT,
            "--window-size=1600,900",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--no-default-browser-check",
            "--user-data-dir=%s" % udd,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as sess:
            if not await wait_endpoint(sess):
                raise SystemExit("CDP endpoint not reachable")
            async with sess.get("http://127.0.0.1:%d/json/list" % PORT) as r:
                targets = await r.json()
            page = next(t for t in targets if t.get("type") == "page")
            async with sess.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=64 * 1024 * 1024) as ws:
                c = CDP(ws)
                await c.call("Page.enable")
                await c.call("Page.navigate", url=URL)
                await asyncio.sleep(3)
                # 点击 Connect
                await c.eval("document.getElementById('btn-connect').click()")
                state = None
                for _ in range(60):
                    await asyncio.sleep(1)
                    state = await c.eval("document.getElementById('state-text').textContent")
                    if state == "Connected":
                        break
                print("STATE:", state)
                # 等统计数字出现(fps 出值且分辨率非 0x0),再等几秒让码率爬升
                fps = None
                for _ in range(10):
                    await asyncio.sleep(4)
                    fps = await c.eval("document.getElementById('stat-fps').textContent")
                    res = await c.eval("document.getElementById('stat-res').textContent")
                    if fps and not fps.startswith("--") and res != "0x0":
                        break
                await asyncio.sleep(5)
                info = await c.eval(
                    "JSON.stringify({state:document.getElementById('state-text').textContent,"
                    "fps:document.getElementById('stat-fps').textContent,"
                    "br:document.getElementById('stat-br').textContent,"
                    "res:document.getElementById('stat-res').textContent,"
                    "log:document.getElementById('log').textContent})"
                )
                print("INFO:", info)
                # 视频区域覆盖标准桌面素材(避免截到真实桌面);参数传 none 可禁用
                overlay = sys.argv[1] if len(sys.argv) > 1 else OVERLAY_IMG
                if overlay.lower() != "none":
                    if not os.path.exists(overlay):
                        raise SystemExit("overlay image not found: %s (pass 'none' to disable)" % overlay)
                    runner = await start_overlay_server(overlay)
                    try:
                        await c.eval(OVERLAY_JS)
                        ok = False
                        for _ in range(40):
                            await asyncio.sleep(0.25)
                            ok = await c.eval("!!(window.__ovl&&window.__ovl.complete&&window.__ovl.naturalWidth)")
                            if ok:
                                break
                        print("OVERLAY:", ok, overlay)
                    finally:
                        await runner.cleanup()
                await asyncio.sleep(2)
                shot = await c.call("Page.captureScreenshot", format="png")
                data = base64.b64decode(shot["data"])
                with open(OUT, "wb") as f:
                    f.write(data)
                print("SAVED: %s (%d bytes)" % (OUT, len(data)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(udd, ignore_errors=True)


asyncio.run(main())