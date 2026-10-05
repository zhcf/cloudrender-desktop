"""CloudRender 服务端内置 Web UI:把 client/web/ 页面(CloudRender Desktop)与
client/javascript/src(SDK) 以静态路由挂进 aiohttp 应用。

页面与 /ws 信令同端口同源:浏览器打开 http://<ip>:<port>/ 即用,
信令地址由页面自动取同源(免手工填写),防火墙只需放行一个端口。

页面源码仅此一份(client/web/ 目录,file:// 直开也可用),由各服务端
(8080 aiortc 版 / 8081 C++ 会话核心版)各自接入托管(本文件在
cloudrender 包与 cpp shell 各一份,须同步修改)。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from aiohttp import web

logger = logging.getLogger("cloudrender.webui")

# server/python/cloudrender/webui.py -> parents[3] = 仓库根
# (副本:server/cpp/shell/webui.py,修改须两处同步)
# PyInstaller 冻结模式:资源根 = 打包资源目录(_MEIPASS,含 add-data 的
# client/web 与 client/javascript/src);源码模式仍按仓库相对路径定位
if getattr(sys, "frozen", False):
    _RES_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    _RES_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _RES_ROOT / "client" / "web"
_SDK_DIR = _RES_ROOT / "client" / "javascript" / "src"


def resolve_web_dir() -> Optional[Path]:
    """页面目录(含 index.html 时有效);缺失返回 None(降级为仅信令)。"""
    return _WEB_DIR if (_WEB_DIR / "index.html").is_file() else None


def install_web_routes(app: web.Application) -> bool:
    """挂载 Web UI 路由;返回是否挂载成功。

    路由与页面资产相对引用一一对应(app.js 内 SDK 引用为
    "../javascript/src/cloudrender.js",从根路径解析为 /javascript/src/):
      /                 -> client/web/index.html
      /app.js           -> client/web/app.js
      /style.css        -> client/web/style.css
      /javascript/src/* -> client/javascript/src(JS SDK)
    """
    web_dir = resolve_web_dir()
    if web_dir is None:
        logger.warning("Web UI 目录未找到(%s),仅提供 /ws 信令", _WEB_DIR)
        return False

    def _page(name: str, content_type: str):
        async def handler(_request: web.Request) -> web.StreamResponse:
            path = web_dir / name
            if not path.is_file():
                raise web.HTTPNotFound(text=f"{name} not found")
            # 显式 Content-Type:模块脚本的 MIME 不依赖系统注册表推断
            return web.FileResponse(path, headers={"Content-Type": content_type})
        return handler

    app.router.add_get("/", _page("index.html", "text/html; charset=utf-8"))
    app.router.add_get("/app.js", _page("app.js", "text/javascript; charset=utf-8"))
    app.router.add_get("/style.css", _page("style.css", "text/css; charset=utf-8"))
    if _SDK_DIR.is_dir():
        app.router.add_static("/javascript/src", _SDK_DIR)
    else:
        logger.warning("JS SDK 目录未找到(%s),页面将无法加载 SDK", _SDK_DIR)
    return True