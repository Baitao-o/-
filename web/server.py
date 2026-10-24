#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门省钱搭子 —— 可视化界面后端（零依赖，只用 Python 标准库）。

启动：
    python web/server.py
然后浏览器打开 http://127.0.0.1:8808

设计说明
--------
- 不引入 Flask/FastAPI，避免参赛者环境装不上依赖；
- 后端只做三件事：定位门店、拉菜单、按结构求最低价组合，其余交给前端渲染；
- MCP 客户端与规划算法直接复用 src/，保证「网页版」与「命令行版」是同一套逻辑，
  不会出现文档写的和页面跑的不一致。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcp_client import BE_TYPE, ORDER_TYPE, McdMCPClient, McdMCPError  # noqa: E402
from meal_planner import (  # noqa: E402
    NON_FOOD_WORDS,
    MealPlanner,
    _build_reasons,
    normalize_meals,
)

PORT = int(os.environ.get("PORT", "8808"))
HOST = os.environ.get("HOST", "127.0.0.1")

_client: McdMCPClient | None = None
_client_lock = threading.Lock()


def get_client() -> McdMCPClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = McdMCPClient()
        return _client


def resolve_stores(mode: str, city: str, keyword: str, address_index: int = 0):
    """定位门店，返回 (stores, be_type)。"""
    c = get_client()
    be_type = BE_TYPE[mode]
    if mode in ("delivery", "catering"):
        addrs = c.addresses()
        if not addrs:
            raise ValueError("该账号没有可用外送地址。请改用「到店自取」并填写城市 + 关键词。")
        stores = c.delivery_stores(addrs[address_index]["addressId"], be_type)
    else:
        if not city or not keyword:
            raise ValueError("到店搜索需要填写「城市」和「位置关键词」。")
        stores = c.nearby_stores(be_type, search_type=2, city=city, keyword=keyword)
    if not stores:
        raise ValueError("没有找到可下单门店，换个关键词或城市试试。")
    return stores, be_type


def plan_to_payload(plan, structure: dict[str, int] | None = None) -> dict:
    """把 Plan 对象转成前端可用的结构。"""
    data = plan.to_dict()
    breakdown = getattr(plan, "price_breakdown", None)
    if breakdown:
        data["breakdown"] = {k: round(v, 2) for k, v in breakdown.items()}
    # 给每个明细项补上它属于哪个品类槽位，前端好分组显示
    items = []
    for it, q in plan.items:
        items.append({
            "code": it.code,
            "name": it.name,
            "quantity": q,
            "unitPrice": round(it.price, 2),
            "subtotal": round(it.price * q, 2),
            "slot": MealPlanner.SLOT_OF_CATEGORY.get(it.category, "other"),
            "slotLabel": MealPlanner.SLOT_LABEL.get(
                MealPlanner.SLOT_OF_CATEGORY.get(it.category, ""), "其他"
            ),
            "category": it.category,
        })
    data["items"] = items
    return data


def api_plan(payload: dict) -> dict:
    """核心接口：按预算（可选结构）出方案。"""
    try:
        budget = float(payload.get("budget") or 0)
    except (TypeError, ValueError):
        raise ValueError("预算必须是数字")
    if budget <= 0:
        raise ValueError("请填写大于 0 的预算")

    people = max(1, int(payload.get("people") or 1))
    mode = payload.get("mode") or "pickup"
    if mode not in BE_TYPE:
        raise ValueError(f"不支持的取餐方式：{mode}")
    city = (payload.get("city") or "").strip()
    keyword = (payload.get("keyword") or "").strip()
    avoid = [w.strip() for w in str(payload.get("avoid") or "").split(",") if w.strip()]
    no_spicy = bool(payload.get("noSpicy"))

    structure = {}
    for key in ("main", "snack", "dessert", "drink"):
        try:
            n = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            structure[key] = n
    if structure and payload.get("perPerson"):
        structure = {k: v * people for k, v in structure.items()}

    stores, be_type = resolve_stores(mode, city, keyword,
                                     int(payload.get("addressIndex") or 0))
    store = stores[0]
    store_code = store.get("storeCode")
    be_code = store.get("beCode")
    store_name = store.get("storeName") or store_code
    order_type = ORDER_TYPE[mode]
    business = store.get("businessStatus")

    c = get_client()
    menu = c.meals(store_code, be_type=be_type, be_code=be_code, order_type=order_type)
    items = normalize_meals(menu)
    if not items:
        raise ValueError("该门店当前没有可售商品")

    planner = MealPlanner(c, items)
    result: dict = {
        "ok": True,
        "store": {
            "storeCode": store_code,
            "beCode": be_code,
            "name": store_name,
            "address": store.get("address") or "",
            "businessStatus": business,
        },
        "request": {
            "budget": budget, "people": people, "mode": mode,
            "city": city, "keyword": keyword, "avoid": avoid,
            "noSpicy": no_spicy, "structure": structure,
        },
        "itemPoolSize": len(items),
    }

    if structure:
        # ===== 结构模式：求最低价 =====
        planner.calibrate_categories(
            store_code, be_type=be_type, be_code=be_code, order_type=order_type, per_cat=6
        )
        cands, err = planner.structure_candidates(
            structure, budget, avoid=avoid, no_spicy=no_spicy
        )
        if err:
            raise ValueError(err)

        verify_n = min(len(cands), 12)
        for p in cands[:verify_n]:
            planner.verify(p, store_code, be_type=be_type, be_code=be_code, order_type=order_type)
            p.per_capita = p.total() / max(1, sum(structure.values()))

        verified = [p for p in cands[:verify_n] if p.verified_total is not None]
        within = [p for p in verified if p.total() <= budget]
        if within:
            best = min(within, key=lambda p: p.total())
            feasible = True
        elif verified:
            best = min(verified, key=lambda p: p.total())
            feasible = False
        else:
            best = cands[0]
            feasible = False

        _build_reasons([best], sum(structure.values()), budget)
        result.update({
            "mode": "structure",
            "feasible": feasible,
            "structure": structure,
            "best": plan_to_payload(best),
            "remaining": round(budget - best.total(), 2),
            "verifiedCount": verify_n,
        })
    else:
        # ===== 默认模式：给 2~3 个方案 =====
        planner.calibrate(
            store_code, be_type=be_type, be_code=be_code, order_type=order_type, limit=22
        )
        plans = planner.generate(
            people=people, budget=budget, avoid=avoid, no_spicy=no_spicy,
            want_drink=True, want_dessert=False, top_n=12,
        )
        if not plans:
            raise ValueError("没有找到可行方案，试试提高预算或放宽限制")

        for p in plans:
            planner.verify(p, store_code, be_type=be_type, be_code=be_code, order_type=order_type)
            p.per_capita = p.total() / max(1, people)
        within = [p for p in plans if p.total() <= budget]
        over = sorted([p for p in plans if p.total() > budget], key=lambda p: p.total())
        if within:
            within_sorted = sorted(within, key=lambda p: p.total())
            selected, used = [], set()
            for p in within_sorted:
                if len(selected) >= 3:
                    break
                if p.title in used:
                    continue
                selected.append(p)
                used.add(p.title)
            for p in within_sorted:
                if len(selected) >= 3:
                    break
                if p not in selected:
                    selected.append(p)
            feasible = True
        else:
            selected = over[:2]
            feasible = False

        _build_reasons(selected, people, budget)
        result.update({
            "mode": "budget",
            "feasible": feasible,
            "plans": [plan_to_payload(p) for p in selected],
        })

    result["notice"] = (
        f"价格与可售状态来自麦当劳 MCP 实时返回（门店 {store_code}，"
        f"{len(items)} 个可售商品），最终以下单页面为准。"
    )
    return result


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MaiMenBudgetMeal/1.0"

    def log_message(self, fmt, *args):  # 安静一点，只打印错误
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[http] " + (fmt % args) + "\n")

    # ---------- 工具 ----------

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        if rel in ("", "index.html"):
            rel = "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR):        # 防目录穿越
            return self._json(403, {"ok": False, "error": "forbidden"})
        if not os.path.isfile(full):
            return self._json(404, {"ok": False, "error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---------- 路由 ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "maimen-budget-meal"})
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/plan":
            return self._json(404, {"ok": False, "error": "not found"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            # 正常浏览器一定发 UTF-8；这里兼容 Windows 终端 curl 的 GBK 字节，
            # 方便开发者命令行调试，不影响前端。
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
            payload = json.loads(text or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"ok": False, "error": f"请求格式错误：{exc}"})

        try:
            return self._json(200, api_plan(payload))
        except (ValueError, McdMCPError) as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": f"服务器内部错误：{exc}"})


def main() -> int:
    try:
        get_client()
    except McdMCPError as exc:
        print(f"[警告] {exc}")
        print("        页面可以打开，但查询会失败；请先配置 MCD_MCP_TOKEN。\n")

    url = f"http://{HOST}:{PORT}/"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 56)
    print("  麦门省钱搭子 · 可视化界面")
    print(f"  打开：{url}")
    print("  Ctrl+C 停止")
    print("=" * 56)
    if os.environ.get("OPEN_BROWSER", "1") == "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
