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

from mcp_client import (  # noqa: E402
    BE_TYPE,
    CONFIG_FILE,
    ORDER_TYPE,
    McdMCPClient,
    McdMCPError,
    clear_token,
    load_token_with_source,
    mask_token,
    save_token,
)
from meal_planner import (  # noqa: E402
    NON_FOOD_WORDS,
    MealPlanner,
    _build_reasons,
    match_want,
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
    want = [w.strip() for w in str(payload.get("want") or "").split(",") if w.strip()]
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
            "noSpicy": no_spicy, "structure": structure, "want": want,
        },
        "itemPoolSize": len(items),
    }

    if structure:
        # ===== 结构模式：求最低价 =====
        planner.calibrate_categories(
            store_code, be_type=be_type, be_code=be_code, order_type=order_type, per_cat=6
        )
        cands, err = planner.structure_candidates(
            structure, budget, avoid=avoid, no_spicy=no_spicy, want=want
        )
        if err:
            # 「配不齐」不是服务器错误，而是合法的业务结论。
            # SKILL 规范要求如实给出最低所需金额，因此返回结构化结果让前端专门展示，
            # 而不是抛 400 让用户只看到一行红字。
            result.update({
                "mode": "structure",
                "feasible": False,
                "infeasible": True,
                "reason": err.get("kind"),
                "error": err.get("message"),
                "minimumNeeded": err.get("minimumNeeded"),
                "gap": err.get("gap"),
                "structure": structure,
                "notice": ("该结构在当前门店无法用这个预算配齐；"
                           "以上为按门店当前可售商品估算的最低所需金额，最终以下单页面为准。"),
                "wantDiagnosis": planner.diagnose_want(want, avoid=avoid, no_spicy=no_spicy),
            })
            return result

        verify_n = min(len(cands), 12)
        for p in cands[:verify_n]:
            planner.verify(p, store_code, be_type=be_type, be_code=be_code, order_type=order_type)
            p.per_capita = p.total() / max(1, sum(structure.values()))

        verified = [p for p in cands[:verify_n] if p.verified_total is not None]
        within = [p for p in verified if p.total() <= budget]

        def _want_hits(pl):
            return sum(len(match_want(it, want)) for it, _ in pl.items)

        # 想吃优先：预算内命中「想吃」的方案优先（即使更贵），并说明价差
        best = None
        if within and want:
            preferred = [p for p in within if _want_hits(p) > 0]
            if preferred:
                best = max(preferred, key=lambda p: (_want_hits(p), -p.total()))
                cheapest_any = min(within, key=lambda p: p.total())
                if best.total() > cheapest_any.total() + 0.01:
                    best.notes.append(
                        f"为满足「{'、'.join(want)}」，实付比同类最低方案"
                        f"（¥{cheapest_any.total():.2f}）高 ¥{best.total() - cheapest_any.total():.2f}"
                    )
        if best is not None:
            feasible = True
        elif within:
            best = min(within, key=lambda p: p.total())
            feasible = True
        elif verified:
            best = min(verified, key=lambda p: p.total())
            feasible = False
        else:
            best = cands[0]
            feasible = False

        _build_reasons([best], sum(structure.values()), budget, want=want)
        result.update({
            "mode": "structure",
            "feasible": feasible,
            "structure": structure,
            "best": plan_to_payload(best),
            "remaining": round(budget - best.total(), 2),
            "verifiedCount": verify_n,
            "wantDiagnosis": planner.diagnose_want(want, avoid=avoid, no_spicy=no_spicy),
        })
    else:
        # ===== 默认模式：给 2~3 个方案 =====
        planner.calibrate(
            store_code, be_type=be_type, be_code=be_code, order_type=order_type, limit=22
        )
        plans = planner.generate(
            people=people, budget=budget, avoid=avoid, no_spicy=no_spicy,
            want_drink=True, want_dessert=False, want=want, top_n=12,
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

        _build_reasons(selected, people, budget, want=want)
        result.update({
            "mode": "budget",
            "feasible": feasible,
            "plans": [plan_to_payload(p) for p in selected],
            "wantDiagnosis": planner.diagnose_want(want, avoid=avoid, no_spicy=no_spicy),
        })

    result["notice"] = (
        f"价格与可售状态来自麦当劳 MCP 实时返回（门店 {store_code}，"
        f"{len(items)} 个可售商品），最终以下单页面为准。"
    )
    return result


# ---------------------------------------------------------------- MCP Token 配置

SOURCE_HINT = {
    "env": "来自环境变量 MCD_MCP_TOKEN（优先级最高）",
    "file": "来自本机配置文件，仅你自己可见",
    "workbuddy": "检测到本机 WorkBuddy 的连接器配置（开发兜底，建议改成自己的）",
    "none": "尚未配置",
}


def api_config_get() -> dict:
    """查询当前 Token 配置状态（只返回掩码，绝不返回明文）。"""
    token, source = load_token_with_source()
    return {
        "ok": True,
        "configured": bool(token),
        "source": source,
        "sourceHint": SOURCE_HINT.get(source, ""),
        "masked": mask_token(token),
        "configPath": CONFIG_FILE,
        "applyUrl": "https://open.mcd.cn/mcp",
        "note": "每个使用者都要配置自己的麦当劳 MCP Token，不能共用同一个。",
    }


def api_config_set(payload: dict) -> dict:
    """保存用户自己的 Token 到本机配置文件，并让客户端立即用新 Token。"""
    global _client
    token = str(payload.get("token") or "").strip()
    if not token:
        raise ValueError("请填写 MCP Token")
    path = save_token(token)
    with _client_lock:
        _client = None  # 丢弃旧客户端缓存，下次请求按新 Token 重建
    return {
        "ok": True,
        "configured": True,
        "configPath": path,
        "masked": mask_token(token),
        "message": "已保存，之后所有查询都会使用这个 Token。",
    }


def api_config_clear() -> dict:
    """删除本机保存的 Token（不影响环境变量）。"""
    global _client
    removed = clear_token()
    with _client_lock:
        _client = None
    token, source = load_token_with_source()
    return {
        "ok": True,
        "removed": removed,
        "configured": bool(token),
        "source": source,
        "message": "已删除本机保存的 Token。" if removed else "本机没有保存过 Token。",
    }


# ---------------------------------------------------------------- 美食转盘

WHEEL_SLOTS = ("main", "snack", "dessert", "drink")
WHEEL_PER_SLOT = 12          # 每个转盘最多放多少个扇形
WHEEL_MIN_CANDIDATES = 4     # 少于此数量就放宽预算约束


def api_wheel_pool(payload: dict) -> dict:
    """生成四个品类转盘的候选池。

    做法：把预算按四个品类均分（budget/4），每类只放「单价不超过该额度」的商品，
    这样四件都转出来后大概率落在预算内。若某类候选不足，则放宽到该类最便宜的若干件，
    由前端在结算时如实提示是否超预算（不做无声裁剪）。

    主食池只取单点汉堡：套餐的菜单价是「最低价起售」，实际成交价更高，
    放进转盘会让用户以为很便宜，转完却超预算。
    """
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

    stores, be_type = resolve_stores(mode, city, keyword,
                                     int(payload.get("addressIndex") or 0))
    store = stores[0]
    store_code = store.get("storeCode")
    be_code = store.get("beCode")
    store_name = store.get("storeName") or store_code
    order_type = ORDER_TYPE[mode]

    c = get_client()
    menu = c.meals(store_code, be_type=be_type, be_code=be_code, order_type=order_type)
    items = normalize_meals(menu)
    if not items:
        raise ValueError("该门店当前没有可售商品")

    slot_budget = budget / len(WHEEL_SLOTS)

    def _clean(lst):
        return [
            it for it in lst
            if not any(w in it.name for w in NON_FOOD_WORDS)
        ]

    pools: dict[str, list[dict]] = {}
    for slot in WHEEL_SLOTS:
        cand = [
            it for it in _clean(items)
            if MealPlanner.SLOT_OF_CATEGORY.get(it.category) == slot
        ]
        if slot == "main":
            # 主食只放单点汉堡，避免套餐「最低价起售」造成价格误判
            single = [it for it in cand if it.category == "汉堡"]
            if len(single) >= WHEEL_MIN_CANDIDATES:
                cand = single
        cand.sort(key=lambda i: i.price)

        affordable = [it for it in cand if it.price <= slot_budget]
        chosen = affordable[:WHEEL_PER_SLOT]
        if len(chosen) < WHEEL_MIN_CANDIDATES:
            # 该品类太贵或太少：放宽到该类最便宜的一批，前端会提示可能超预算
            chosen = cand[:min(WHEEL_PER_SLOT, max(WHEEL_MIN_CANDIDATES, len(cand)))]

        pools[slot] = [
            {
                "code": it.code,
                "name": it.name,
                "price": round(it.price, 2),
                "originalPrice": round(it.original_price, 2),
                "category": it.category,
            }
            for it in chosen
        ]

    empty = [MealPlanner.SLOT_LABEL[s] for s in WHEEL_SLOTS if not pools.get(s)]
    if empty:
        raise ValueError(f"该门店没有可用的「{'、'.join(empty)}」类商品")

    return {
        "ok": True,
        "store": {
            "storeCode": store_code,
            "beCode": be_code,
            "name": store_name,
            "address": store.get("address") or "",
            "businessStatus": store.get("businessStatus"),
        },
        "request": {"budget": budget, "people": people, "mode": mode},
        "slotBudget": round(slot_budget, 2),
        "itemPoolSize": len(items),
        "pools": pools,
        "notice": (f"每个转盘的候选都来自门店 {store_code} 的实时菜单（共 {len(items)} 个商品），"
                   "标价为菜单价，最终以真实算价为准。"),
    }


def api_wheel_settle(payload: dict) -> dict:
    """转盘抽完后结算：对抽中的商品做真实算价。

    与结构模式的区别：结构模式是自己求最低价，而转盘是「用户抽到什么就结算什么」，
    因此这里直接调 calculate-price，不做任何替换或优化——
    否则用户会觉得「我抽到的东西被偷偷换掉了」。
    """
    store_code = (payload.get("storeCode") or "").strip()
    if not store_code:
        raise ValueError("缺少门店编码 storeCode")
    mode = payload.get("mode") or "pickup"
    if mode not in BE_TYPE:
        raise ValueError(f"不支持的取餐方式：{mode}")

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("没有要结算的商品")
    calc_items = []
    for it in raw_items:
        code = str((it or {}).get("code") or "").strip()
        qty = int((it or {}).get("quantity") or 1)
        if not code or qty <= 0:
            continue
        calc_items.append({"productCode": code, "quantity": qty})
    if not calc_items:
        raise ValueError("没有有效的商品")

    be_type = BE_TYPE[mode]
    order_type = ORDER_TYPE[mode]
    be_code = (payload.get("beCode") or "").strip() or None

    c = get_client()
    res = c.calculate_price(
        store_code, calc_items, be_type=be_type, be_code=be_code, order_type=order_type
    )
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict):
        raise ValueError("算价返回结构异常")

    fen = lambda v: round((float(v or 0)) / 100.0, 2)  # noqa: E731
    return {
        "ok": True,
        "store": {"storeCode": store_code},
        "price": fen(data.get("price")),
        "originalPrice": fen(data.get("originalPrice")),
        "discount": fen(data.get("discount")),
        "productPrice": fen(data.get("productPrice")),
        "deliveryPrice": fen(data.get("deliveryPrice")),
        "packingPrice": fen(data.get("packingPrice")),
        "products": [
            {
                "code": p.get("productCode"),
                "name": p.get("productName"),
                "quantity": p.get("quantity"),
                "subtotal": fen(p.get("subtotal")),
            }
            for p in (data.get("productList") or [])
        ],
        "notice": "价格来自麦当劳 MCP 的 calculate-price 实时计算，最终以下单页面为准。",
    }


# ---------------------------------------------------------------- 下单与支付跳转
#
# ⚠️ 安全红线（高于功能）：
#   1. /api/order/create 的 dryRun **默认 True**——前端/调用方不显式说"要真实建单"，
#      后端绝不调 create-order，只返回 wouldCreate 预览；
#   2. dryRun=false 必须同时带 confirmed=true，否则 400 拒绝（防前端 bug 或直连接口）；
#   3. takeWayCode 必须来自本次 calculate-price 返回的 takeWayList，拒绝一切硬编码；
#   4. 非食品（贴纸/周边）不能单独建单（官方 code 499 实测），建单前先校验正价食品；
#   5. 不打印、不记录 orderId / payH5Url（log_message 只输出请求行，不含 body）。


def _order_fen(v) -> float:
    """MCP 金额单位是分，统一转成元。"""
    return round((float(v or 0)) / 100.0, 2)


def _order_ctx(payload: dict):
    """解析并校验订单接口的公共入参。"""
    store_code = (payload.get("storeCode") or "").strip()
    if not store_code:
        raise ValueError("缺少门店编码 storeCode")
    try:
        be_type = int(payload.get("beType") or 1)
        order_type = int(payload.get("orderType") or 1)
    except (TypeError, ValueError):
        raise ValueError("beType / orderType 必须是整数")
    if be_type not in BE_TYPE.values():
        raise ValueError(f"不支持的 beType：{be_type}")
    if order_type not in (1, 2):
        raise ValueError(f"不支持的 orderType：{order_type}")
    be_code = (payload.get("beCode") or "").strip() or None
    items = []
    for it in payload.get("items") or []:
        code = str((it or {}).get("code") or (it or {}).get("productCode") or "").strip()
        try:
            qty = int((it or {}).get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 0
        if code and qty > 0:
            items.append({"productCode": code, "quantity": qty})
    if not items:
        raise ValueError("没有有效的商品")
    return store_code, be_type, order_type, be_code, items


def _price_view(data: dict) -> dict:
    """把 calculate-price 的 data 转成前端视图：金额分→元，并透传 takeWayList。"""
    return {
        "price": _order_fen(data.get("price")),
        "originalPrice": _order_fen(data.get("originalPrice")),
        "discount": _order_fen(data.get("discount")),
        "productPrice": _order_fen(data.get("productPrice")),
        "deliveryPrice": _order_fen(data.get("deliveryPrice")),
        "packingPrice": _order_fen(data.get("packingPrice")),
        "products": [
            {
                "code": p.get("productCode"),
                "name": p.get("productName"),
                "quantity": p.get("quantity"),
                "subtotal": _order_fen(p.get("subtotal")),
            }
            for p in (data.get("productList") or [])
        ],
        "takeWayList": [
            {"code": t.get("code"), "title": t.get("title"), "subtitle": t.get("subtitle")}
            for t in (data.get("takeWayList") or [])
        ],
    }


def _food_gate(store_code: str, be_type: int, order_type: int,
               be_code: str | None, items: list[dict]):
    """建单前置校验：订单里至少要有一个正价食品。

    官方限制实测（code 499）：非食品（贴纸、周边）不能单独购买。
    这里用菜单把商品 code 对回名称与价格，判断是否食品，只读不建单。
    """
    c = get_client()
    menu = normalize_meals(
        c.meals(store_code, be_type=be_type, be_code=be_code, order_type=order_type)
    )
    by_code = {it.code: it for it in menu}
    for it in items:
        m = by_code.get(it["productCode"])
        if m and m.price > 0 and not any(w in m.name for w in NON_FOOD_WORDS):
            return
    raise ValueError(
        "无法下单：订单里没有正价食品。非食品（贴纸/周边等）不能单独建单"
        "（麦当劳官方限制：其他餐品金额需大于 0 元），请先加一个汉堡、饮料等食品。"
    )


def api_order_prepare(payload: dict) -> dict:
    """只读预备接口：算价 + 返回 takeWayList（堂食/外带等），不建单。

    前端先调它让用户选「堂食 / 外带」，再进真实订单确认弹窗。
    """
    store_code, be_type, order_type, be_code, items = _order_ctx(payload)
    _food_gate(store_code, be_type, order_type, be_code, items)
    c = get_client()
    res = c.calculate_price(store_code, items, be_type=be_type,
                            be_code=be_code, order_type=order_type)
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict):
        raise ValueError("算价返回结构异常")
    view = _price_view(data)
    return {
        "ok": True,
        "storeCode": store_code,
        "items": items,
        "beType": be_type,
        "orderType": order_type,
        **view,
        "notice": "金额来自麦当劳 MCP calculate-price 实时计算；取餐方式必须从 takeWayList 中选择。",
    }


def api_order_create(payload: dict) -> dict:
    """建单接口（含演示模式开关，dryRun 默认 True，绝不默认真实建单）。"""
    dry_run = payload.get("dryRun", True)
    if not isinstance(dry_run, bool):
        dry_run = True  # 任何非布尔值都按演示处理，宁可误演示不可误下单
    if not dry_run and payload.get("confirmed") is not True:
        raise ValueError(
            "真实建单被拒绝：dryRun=false 时必须同时传 confirmed=true"
            "（防前端 bug 或绕过确认弹窗直接调接口）"
        )

    store_code, be_type, order_type, be_code, items = _order_ctx(payload)
    _food_gate(store_code, be_type, order_type, be_code, items)

    c = get_client()
    res = c.calculate_price(store_code, items, be_type=be_type,
                            be_code=be_code, order_type=order_type)
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict):
        raise ValueError("算价返回结构异常")
    view = _price_view(data)

    # takeWayCode 必须取自本次 calculate-price 的 takeWayList，禁止硬编码
    take_way_code = (payload.get("takeWayCode") or "").strip()
    if order_type == 1:
        valid_codes = [t.get("code") for t in view["takeWayList"]]
        if not take_way_code:
            raise ValueError("缺少 takeWayCode（必须来自 calculate-price 返回的 takeWayList）")
        if valid_codes and take_way_code not in valid_codes:
            raise ValueError(
                f"takeWayCode「{take_way_code}」不在该门店 takeWayList 中"
                f"（可选：{'、'.join(filter(None, valid_codes))}），已拒绝建单"
            )

    remark = (payload.get("remark") or "").strip() or None
    address_id = (payload.get("addressId") or "").strip() or None

    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "wouldCreate": {
                "storeCode": store_code,
                "items": items,
                "takeWayCode": take_way_code or None,
                "price": view["price"],
                "originalPrice": view["originalPrice"],
                "products": view["products"],
            },
            "notice": "演示模式：没有调用 create-order，没有产生真实订单，不占用库存。",
        }

    order = c.create_order(
        store_code, items, take_way_code,
        be_type=be_type, be_code=be_code, order_type=order_type,
        address_id=address_id, remark=remark,
    )
    return {
        "ok": True,
        "dryRun": False,
        "orderId": order.get("orderId"),
        "payId": order.get("payId"),
        "payH5Url": order.get("payH5Url"),
        "orderDetail": order.get("orderDetail") or {},
        "price": view["price"],
        "products": view["products"],
        "notice": "订单已创建（待支付）。付款页为麦当劳官方域名，本作品不经手任何款项。",
    }


def api_order_cancel(payload: dict) -> dict:
    """取消未支付订单。"""
    order_id = str(payload.get("orderId") or "").strip()
    if not order_id:
        raise ValueError("缺少 orderId")
    reason = str(payload.get("cancelReasonCode") or "1")
    data = get_client().cancel_order(order_id, reason)
    ok = bool(data.get("cancelResult"))
    return {
        "ok": True,
        "cancelResult": ok,
        "orderId": order_id,
        "message": ("订单已取消。" if ok
                    else "取消请求已提交但未确认成功，请到麦当劳 App「待付款」里核实。"),
    }


def api_order_query(payload: dict) -> dict:
    """查订单真实状态（只在用户主动点「我已支付」时调用一次，不做自动轮询）。"""
    order_id = str(payload.get("orderId") or "").strip()
    if not order_id:
        raise ValueError("缺少 orderId")
    data = get_client().query_order(order_id)
    return {
        "ok": True,
        "orderId": order_id,
        "orderStatus": data.get("orderStatus"),
        "status": data.get("status"),
        "storeName": data.get("storeName"),
    }


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
        # 允许 DSH 页面的悬浮球插件跨端口探测与拉起（本机回环服务，放开 CORS 无风险）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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

    def do_OPTIONS(self):
        """CORS 预检。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            token, source = load_token_with_source()
            return self._json(200, {
                "ok": True,
                "service": "maimen-budget-meal",
                "mcpConfigured": bool(token),
                "mcpSource": source,
            })
        if path == "/api/config":
            return self._json(200, api_config_get())
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        handlers = {
            "/api/plan": api_plan,
            "/api/wheel/pool": api_wheel_pool,
            "/api/wheel/settle": api_wheel_settle,
            "/api/order/prepare": api_order_prepare,
            "/api/order/create": api_order_create,
            "/api/order/cancel": api_order_cancel,
            "/api/order/query": api_order_query,
            "/api/config": api_config_set,
            "/api/config/clear": lambda _payload: api_config_clear(),
        }
        handler = handlers.get(path)
        if handler is None:
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
            return self._json(200, handler(payload))
        except (ValueError, McdMCPError) as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": f"服务器内部错误：{exc}"})


def _port_in_use(host: str, port: int) -> bool:
    """主动探测端口是否已被占用。

    不能只靠 bind 抛 OSError 来判断：Windows 上 SO_REUSEADDR 的语义与 Linux 不同，
    两个进程可以同时 bind 同一个端口而不报错（实测会出现两个进程都 LISTENING 8808，
    流量被随机分流）。所以必须先主动连一下。
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.35)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def _bind(host: str, start_port: int, tries: int = 12):
    """绑定端口；被占用时自动往后找，省得用户先手动杀进程。"""
    last_exc = None
    for port in range(start_port, start_port + tries):
        if _port_in_use(host, port):
            if port == start_port:
                print(f"[提示] 端口 {start_port} 已被占用，自动尝试后续端口…")
            continue
        try:
            return ThreadingHTTPServer((host, port), Handler), port
        except OSError as exc:
            last_exc = exc
    raise SystemExit(f"无法绑定端口 {start_port}~{start_port + tries - 1}：{last_exc}")


def main() -> int:
    try:
        get_client()
    except McdMCPError as exc:
        print(f"[警告] {exc}")
        print("        页面可以打开，但查询会失败；请先配置 MCD_MCP_TOKEN。\n")

    server, port = _bind(HOST, PORT)
    url = f"http://{HOST}:{port}/"
    print("=" * 56)
    print("  麦门省钱搭子 · 可视化界面")
    print(f"  打开：{url}")
    print("  三个模式：按结构配 / 帮我搭配 / 美食转盘")
    print("  Ctrl+C 停止（或直接关闭窗口）")
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
