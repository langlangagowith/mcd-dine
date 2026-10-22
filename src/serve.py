#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcd-dine 本地服务：把配餐内核包成 HTTP API，并托管单文件前端。

    python run.py            # → http://127.0.0.1:8777

只监听 127.0.0.1（token 在服务端，不发给浏览器）。
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mcp_client  # noqa: E402
from dine_service import DineError, DineService  # noqa: E402

PORT = int(os.environ.get("MCD_DINE_PORT", "8777"))
SVC: DineService | None = None
LAST_TRACE: list[dict] = []


def svc() -> DineService:
    global SVC
    if SVC is None:
        SVC = DineService()
    return SVC


class Handler(BaseHTTPRequestHandler):
    server_version = "mcd-dine/0.1"
    protocol_version = "HTTP/1.1"

    # -- 基础 ---------------------------------------------------------------
    def log_message(self, fmt, *args):  # 安静一点，别刷屏
        if os.environ.get("MCD_DINE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            try:
                s = svc()
                info = getattr(s.cli, "server_info", {})
                return self._json({"ok": True, "mode": "live", "server": info,
                                   "url": mcp_client.DEFAULT_URL})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "mode": "live", "error": str(e)}, 503)
        if path == "/api/stores":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            kw = (q.get("keyword") or [""])[0]
            city = (q.get("city") or ["杭州市"])[0]
            try:
                stores = svc().find_stores(city, kw)
                return self._json({"ok": True, "stores": stores})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._static(path)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/recommend":
                return self._recommend()
            if path == "/api/claim":
                return self._claim()
            if path == "/api/order":
                return self._order()
        except DineError as e:
            return self._json({"ok": False, "error": str(e), "hint": e.hint}, 422)
        except mcp_client.ToolError as e:
            return self._json({"ok": False, "error": str(e), "code": e.code}, 502)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": False, "error": "not found"}, 404)

    # -- 业务 ---------------------------------------------------------------
    def _recommend(self):
        req = self._body()
        res = svc().recommend(req)
        return self._json(res)

    def _claim(self):
        res = svc().claim_coupons()
        return self._json(res)

    def _order(self):
        """建单前再核一次价：前端传来的金额和实际核价不一致就拒绝。"""
        req = self._body()
        store = {"storeCode": str(req.get("storeCode") or "")}
        items = req.get("items") or []
        if not store["storeCode"] or not items:
            return self._json({"ok": False, "error": "缺少门店或商品"}, 422)
        order_type = 1 if req.get("scene") != "delivery" else 2
        be_type = 1 if order_type == 1 else 2
        if req.get("beCode"):
            store["beCode"] = req["beCode"]

        # 只把白名单字段传给接口
        payload = [{
            "productCode": str(i.get("code")),
            "quantity": max(1, int(i.get("qty") or 1)),
            **({"couponId": i["couponId"], "couponCode": i["couponCode"]}
               if i.get("couponId") and i.get("couponCode") else {}),
        } for i in items]

        data = svc().price(store, payload, order_type, be_type)
        real_total = round(float(data.get("price") or 0) / 100, 2)
        expect = req.get("expectTotal")
        if expect is not None and abs(real_total - float(expect)) > 0.01:
            return self._json({
                "ok": False,
                "error": f"价格变了：下单前核价 {real_total:.2f} 元，和刚才显示的 {float(expect):.2f} 元不一致",
                "hint": "可能是券被用掉或促销结束，请重新配一次",
                "revalidated": real_total,
            }, 409)

        plan = type("P", (), {"items": []})()
        from dine_service import Item, Meal, Plan  # 局部导入，避免污染
        its = []
        for i in payload:
            m = Meal(code=i["productCode"], name="", price=0, orig_price=0, role="main", category="")
            it = Item(m, i["quantity"])
            it.coupon = ({"couponId": i.get("couponId"), "couponCode": i.get("couponCode")}
                         if i.get("couponId") else None)
            its.append(it)
        plan = Plan(items=its, template="alacarte")
        take_way = req.get("takeWayCode") or "take-in-store"
        out = svc().create_order(store, plan, take_way, order_type, be_type,
                                 str(req.get("remark") or ""))
        return self._json({"ok": True, "order": out, "revalidated": real_total})

    # -- 静态 ---------------------------------------------------------------
    def _static(self, path: str):
        if path in ("/", "/index.html"):
            f = os.path.join(ROOT, "index.html")
        elif path.startswith("/web/"):
            f = os.path.join(ROOT, path.lstrip("/"))
        else:
            f = os.path.join(ROOT, path.lstrip("/"))
        f = os.path.abspath(f)
        if not f.startswith(ROOT) or not os.path.isfile(f):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        }.get(os.path.splitext(f)[1].lower(), "application/octet-stream")
        with open(f, "rb") as fh:
            self._send(200, fh.read(), ctype)


def main() -> int:
    # 启动时先握手，失败也能起服务（前端会显示离线模式）
    try:
        s = svc()
        info = getattr(s.cli, "server_info", {})
        print(f"· MCP 已连接 {info.get('name')} {info.get('version')} → {mcp_client.DEFAULT_URL}")
    except Exception as e:  # noqa: BLE001
        print(f"! MCP 连接失败：{e}")
        print("  前端会退回离线快照模式；检查 .env.local 里的 MCD_MCP_TOKEN")

    url = f"http://127.0.0.1:{PORT}/"
    print(f"· mcd-dine 已启动 → {url}")
    if os.environ.get("MCD_DINE_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n· bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
