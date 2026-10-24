#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门省钱搭子 —— 组合规划引擎。

把 `mcp_client.McdMCPClient` 拿到的**真实**门店/菜单/优惠数据，
变成 2～3 个可解释、可执行的组合方案。

算法：硬约束过滤 → 品类分组 → 候选枚举 → 加权评分 → 真实算价校验

设计原则
--------
1. 硬约束（预算、忌口、忌辣）**不可违反**：不满足就直接不出方案，而不是"尽量满足"。
2. 本地用菜单里的 currentPrice 做快速枚举，只对最终候选调用 `calculate-price`
   做真实算价校验 —— 既保证真实性，又避免把 MCP 打爆。
3. 每个方案都必须能说清「为什么是它」，禁止只给商品名。

单独运行（需要已配置 MCP Token）：
    python src/meal_planner.py --people 2 --budget 60 --city 绍兴 --keyword 上虞
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")

# Windows 控制台默认 GBK，统一强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream.encoding and _stream.encoding.lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


try:
    from mcp_client import BE_TYPE, ORDER_TYPE, McdMCPClient, McdMCPError
except ImportError:  # 直接以脚本方式运行时的兜底
    from mcp_client import BE_TYPE, ORDER_TYPE, McdMCPClient, McdMCPError  # type: ignore


# ---------------------------------------------------------------- 数据模型

# 品类关键词（按 priority 顺序匹配商品名/分类名）
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("套餐", ("餐", "套餐", "三件套", "四件套", "分享", "桶", "盒")),
    ("汉堡", ("堡", "汉堡", "巨无霸", "安格斯", "麦辣鸡腿", "板烧")),
    ("小食", ("薯条", "薯饼", "鸡块", "鸡翅", "鸡腿", "洋葱圈", "派", "玉米", "沙拉", "小食")),
    ("饮料", ("可乐", "雪碧", "咖啡", "茶", "果汁", "牛奶", "奶昔", "饮", "水", "摩卡", "拿铁")),
    ("甜品", ("冰淇淋", "冰激凌", "甜筒", "新地", "麦旋风", "布丁", "蛋糕", "甜品")),
]

# 忌口关键词 -> 命中即排除
SPICY_WORDS = ("辣", "麻辣", "香辣", "韩式辣椒", "椒")
MEAT_WORDS = ("牛", "堡", "鸡", "鱼", "肉", "猪", "虾")


@dataclass
class Item:
    """菜单里的一个可售商品。"""

    code: str
    name: str
    price: float
    original_price: float
    can_order: bool
    categories: list[str] = field(default_factory=list)

    @property
    def category(self) -> str:
        for cat, words in CATEGORY_RULES:
            if any(w in self.name for w in words):
                return cat
        return "其他"

    @property
    def discount_ratio(self) -> float:
        """原价/现价，越大越划算；无原价时视为 1。"""
        if self.price <= 0 or self.original_price <= 0:
            return 1.0
        return self.original_price / self.price

    @property
    def saved(self) -> float:
        return max(0.0, self.original_price - self.price)


@dataclass
class Plan:
    """一个候选方案。"""

    key: str
    title: str
    reason: str
    items: list[tuple[Item, int]]          # (商品, 数量)
    subtotal: float                        # 本地估算总价
    verified_total: float | None = None    # calculate-price 真实总价
    verified_saved: float | None = None
    per_capita: float | None = None
    notes: list[str] = field(default_factory=list)

    def total(self) -> float:
        return self.verified_total if self.verified_total is not None else self.subtotal

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "reason": self.reason,
            "items": [
                {"code": it.code, "name": it.name, "quantity": q, "unitPrice": it.price}
                for it, q in self.items
            ],
            "subtotal": round(self.subtotal, 2),
            "verifiedTotal": None if self.verified_total is None else round(self.verified_total, 2),
            "verifiedSaved": None if self.verified_saved is None else round(self.verified_saved, 2),
            "perCapita": None if self.per_capita is None else round(self.per_capita, 2),
            "notes": self.notes,
        }


# ---------------------------------------------------------------- 解析工具

def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(str(v).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def normalize_meals(menu_data: dict) -> list[Item]:
    """把 query-meals 的原始返回转成 Item 列表（去重、过滤无效价格）。"""
    raw_meals = (menu_data or {}).get("meals") or {}
    cat_index: dict[str, list[str]] = {}
    for cat in (menu_data or {}).get("categories") or []:
        cname = (cat.get("name") or "").replace("\n", "")
        for m in cat.get("meals") or []:
            code = str(m.get("code") or "")
            if code:
                cat_index.setdefault(code, []).append(cname)

    items: list[Item] = []
    seen: set[str] = set()
    for code, raw in raw_meals.items():
        code = str(code)
        if code in seen:
            continue
        seen.add(code)
        name = (raw.get("name") or "").strip()
        price = _to_float(raw.get("currentPrice"))
        if not name or price <= 0:
            continue
        items.append(
            Item(
                code=code,
                name=name,
                price=price,
                original_price=_to_float(raw.get("originalPrice")),
                can_order=bool(raw.get("canWithOrder", True)),
                categories=cat_index.get(code, []),
            )
        )
    return items


def match_avoid(name: str, avoid: Iterable[str]) -> bool:
    """商品名是否命中忌口关键词。"""
    return any(w and w in name for w in avoid)


def is_spicy(item: Item) -> bool:
    return any(w in item.name for w in SPICY_WORDS)


# ---------------------------------------------------------------- 规划器

class MealPlanner:
    """在真实菜单上生成 3 个策略不同的组合方案。"""

    def __init__(self, client: McdMCPClient, items: list[Item]):
        self.client = client
        self.items = items
        # 注意：`query-meals` 的 canWithOrder 在本门店只对 16 个套餐为 true，
        # 但实测单品（汉堡/小食/饮料）同样能被 calculate-price 正常算价，
        # 因此**不能**用它当「能否下单」的硬过滤，否则会只剩套餐、方案多样性归零。
        # 真正的权威是 calculate-price：算不出价的组合会在 verify 阶段被标记出来。
        self.pool = list(items)

    # ---------- 硬约束 ----------

    def filter_hard(
        self,
        avoid: Iterable[str] = (),
        no_spicy: bool = False,
        max_unit_price: float | None = None,
    ) -> list[Item]:
        out = []
        for it in self.pool:
            if match_avoid(it.name, avoid):
                continue
            if no_spicy and is_spicy(it):
                continue
            if max_unit_price is not None and it.price > max_unit_price:
                continue
            out.append(it)
        return out

    # ---------- 评分 ----------

    @staticmethod
    def score(plan: Plan, people: int, budget: float, want_drink: bool,
              want_dessert: bool) -> float:
        """加权评分：预算贴合 + 人均合理 + 优惠力度 + 品类完整 + 多样性。"""
        total = plan.total()
        s = 0.0

        # 1) 预算贴合：越接近但不超预算越好
        if total > budget:
            s -= 100 + (total - budget) * 3
        else:
            fill = total / budget if budget else 0
            s += 40 * (1 - abs(1 - fill) ** 1.5)

        # 2) 人均份量合理性（每人至少 1 个主食级商品）
        mains = sum(q for it, q in plan.items if it.category in ("套餐", "汉堡"))
        s += 20 * min(1.0, mains / max(1, people))

        # 3) 优惠力度
        saved = sum(it.saved * q for it, q in plan.items)
        s += min(25.0, saved * 0.8)

        # 4) 品类完整
        cats = {it.category for it, _ in plan.items}
        if want_drink and "饮料" in cats:
            s += 10
        if want_dessert and "甜品" in cats:
            s += 10
        s += 6 * len(cats)

        # 5) 份量冗余惩罚：人均过高或过低都扣
        per = total / max(1, people)
        if per > budget / max(1, people) * 1.05:
            s -= 15
        return s

    # ---------- 生成 ----------

    def generate(
        self,
        people: int,
        budget: float,
        avoid: Iterable[str] = (),
        no_spicy: bool = False,
        want_drink: bool = True,
        want_dessert: bool = False,
        max_candidates: int = 4000,
        top_n: int = 12,
    ) -> list[Plan]:
        """枚举候选组合，返回按估算分排序的前 top_n 个。

        注意：菜单里的 currentPrice 与 calculate-price 的真实成交价可能不一致
        （前者偏低的常见原因是最低价起售/未计入套餐实际 SKU），因此这里只做**初筛**，
        真实去留由调用方 verify() 后的真实总价决定。
        生成条件 subtotal <= budget 是充分的：真实价 ≥ 菜单价，
        所以「真实价 ≤ 预算」的组合必然也满足「菜单价 ≤ 预算」。
        """
        pool = self.filter_hard(avoid, no_spicy, max_unit_price=budget)
        if not pool:
            return []

        combos: list[list[tuple[Item, int]]] = []
        single_budget = budget / max(1, people)

        # --- 策略 1：单点套餐（每人一份套餐级商品）---
        sets = [i for i in pool if i.category == "套餐" and i.price <= budget]
        sets.sort(key=lambda i: i.price)
        for s in sets[:120]:
            q = max(1, people)
            if s.price * q <= budget:
                combos.append([(s, q)])

        # --- 策略 2：套餐 + 小食 + 饮料 ---
        snacks = sorted([i for i in pool if i.category == "小食"], key=lambda i: i.price)[:40]
        drinks = sorted([i for i in pool if i.category == "饮料"], key=lambda i: i.price)[:40]
        desserts = sorted([i for i in pool if i.category == "甜品"], key=lambda i: i.price)[:30]
        for s in sets[:60]:
            base = s.price * max(1, people)
            for sn in snacks[:12]:
                for dr in (drinks[:10] or [None]):
                    extra = (sn.price if sn else 0) + (dr.price if dr else 0)
                    total = base + extra
                    if total <= budget:
                        combo = [(s, max(1, people))]
                        if sn:
                            combo.append((sn, 1))
                        if dr:
                            combo.append((dr, max(1, people)))
                        combos.append(combo)
                        if len(combos) > max_candidates:
                            break

        # --- 策略 3：汉堡单点拼配，饮料/小食/甜品按需叠加（不强制带）---
        burgers = sorted([i for i in pool if i.category == "汉堡"], key=lambda i: i.price)[:60]
        for b in burgers[:25]:
            bq = max(1, people)
            base = b.price * bq
            if base > budget:
                continue
            combos.append([(b, bq)])                                    # 只要主食
            for sn in snacks[:8]:                                       # + 小食
                if base + sn.price <= budget:
                    combos.append([(b, bq), (sn, 1)])
            for dr in drinks[:8]:                                       # + 饮料
                if base + dr.price * bq <= budget:
                    combos.append([(b, bq), (dr, bq)])
            for sn in snacks[:6]:                                       # + 小食 + 饮料
                for dr in drinks[:6]:
                    if base + sn.price + dr.price * bq <= budget:
                        combos.append([(b, bq), (sn, 1), (dr, bq)])
            for sn in snacks[:4]:                                       # + 小食 + 饮料 + 甜品
                for dr in drinks[:4]:
                    for ds in desserts[:4]:
                        if base + sn.price + (dr.price + ds.price) * bq <= budget:
                            combos.append([(b, bq), (sn, 1), (dr, bq), (ds, bq)])

        if not combos:
            return self._fallback(people, budget, pool)

        # --- 去重 + 打分 ---
        scored: list[tuple[float, Plan]] = []
        seen_keys: set[str] = set()
        for combo in combos:
            key = "|".join(f"{it.code}x{q}" for it, q in combo)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            subtotal = sum(it.price * q for it, q in combo)
            cats = {it.category for it, _ in combo}
            if "套餐" in cats:
                title = "套餐组合"
            elif "汉堡" in cats:
                title = "单点拼配"
            else:
                title = "小食饮品"
            plan = Plan(
                key=key, title=title, reason="", items=combo, subtotal=subtotal,
                per_capita=subtotal / max(1, people),
            )
            plan.score = self.score(plan, people, budget, want_drink, want_dessert)  # type: ignore[attr-defined]
            scored.append((plan.score, plan))  # type: ignore[attr-defined]

        scored.sort(key=lambda x: x[0], reverse=True)

        # --- 选出 top_n 个方案：先按品类轮转保证多样性，再按分数补齐 ---
        by_title: dict[str, list[Plan]] = {}
        for _score, plan in scored:
            by_title.setdefault(plan.title, []).append(plan)

        chosen: list[Plan] = []
        seen_code_sets: list[frozenset[str]] = []
        while len(chosen) < top_n and any(by_title.values()):
            progressed = False
            for title in list(by_title.keys()):
                bucket = by_title.get(title) or []
                if not bucket:
                    by_title.pop(title, None)
                    continue
                plan = bucket.pop(0)
                codes = frozenset(it.code for it, _ in plan.items)
                # 与已选方案高度重合则跳过，保持方案之间差异明显
                if any(len(codes & prev) >= len(codes) * 0.8 for prev in seen_code_sets):
                    progressed = True
                    continue
                chosen.append(plan)
                seen_code_sets.append(codes)
                progressed = True
                if len(chosen) >= top_n:
                    break
            if not progressed:
                break
        return chosen

    # ---------- 兜底 ----------

    def _fallback(self, people: int, budget: float, pool: list[Item]) -> list[Plan]:
        """预算内无解时，返回「最低可行配置」并明确说明还差多少钱。

        宁可如实告诉用户预算不够，也不硬凑一个超预算方案当作可行解。
        """
        mains = [i for i in pool if i.category in ("套餐", "汉堡")] or pool
        cheapest = sorted(mains, key=lambda i: i.price)[:2]
        out: list[Plan] = []
        for it in cheapest:
            q = max(1, people)
            subtotal = it.price * q
            gap = subtotal - budget
            plan = Plan(
                key=f"fallback|{it.code}x{q}",
                title="最低可行配置",
                reason=f"预算内没有任何组合可行；这是最便宜的配置，仍差约 ¥{gap:.0f}",
                items=[(it, q)],
                subtotal=subtotal,
                per_capita=subtotal / q,
            )
            plan.notes.append(
                f"预算 ¥{budget:.0f} 不足以覆盖 {people} 人最低配置（约 ¥{subtotal:.0f}，未含配送/打包费），"
                f"建议至少提高到 ¥{subtotal:.0f} 以上"
            )
            out.append(plan)
        return out

    # ---------- 真实单价校准 ----------

    def calibrate(self, store_code: str, be_type: int = 1, be_code: str | None = None,
                  order_type: int | None = None, limit: int = 22) -> int:
        """用真实算价校准一批商品的单价，返回被修正的商品数。

        为什么需要：`query-meals` 返回的 currentPrice 对套餐类商品往往只是
        「最低价起售」（例如四件套菜单价 ¥28，实际成交 ¥52），若直接用它做预算
        筛选，会得到「看起来在预算内、实际远超」的假方案。
        因此对最便宜的一批候选逐个调用 calculate-price 探真实单价。
        """
        candidates = sorted(self.pool, key=lambda i: i.price)[:limit]
        fixed = 0
        for it in candidates:
            try:
                res = self.client.calculate_price(
                    store_code, [{"productCode": it.code, "quantity": 1}],
                    be_type=be_type, be_code=be_code, order_type=order_type,
                )
            except McdMCPError:
                continue
            data = res.get("data") if isinstance(res, dict) else None
            if not isinstance(data, dict):
                continue
            real = _to_float(data.get("productPrice")) / 100.0
            orig = _to_float(data.get("originalPrice")) / 100.0
            if real > 0 and abs(real - it.price) > 0.01:
                it.price = real
                fixed += 1
            if orig > 0:
                it.original_price = orig
        return fixed

    # ---------- 真实算价 ----------

    def verify(self, plan: Plan, store_code: str, be_type: int = 1,
               be_code: str | None = None, order_type: int | None = None) -> Plan:
        """调用 calculate-price 用真实优惠规则校验总价。

        MCP 的价格字段单位是**分**（如 price=5900 表示 ¥59.00），此处统一换算成元。
        外送场景的实付总价 = 商品小计 + 配送费 + 打包费 - 优惠。
        """
        payload_items = [
            {"productCode": it.code, "quantity": q} for it, q in plan.items
        ]
        try:
            res = self.client.calculate_price(
                store_code, payload_items, be_type=be_type, be_code=be_code,
                order_type=order_type,
            )
        except McdMCPError as exc:
            plan.notes.append(f"真实算价失败（展示菜单价估算）：{exc}")
            return plan

        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            plan.notes.append("真实算价返回结构异常（展示菜单价估算）")
            return plan

        total_fen = _to_float(data.get("price"))
        if total_fen <= 0:
            plan.notes.append("算价未返回实付总价（展示菜单价估算）")
            return plan

        plan.verified_total = total_fen / 100.0
        plan.price_breakdown = {  # type: ignore[attr-defined]
            "product": _to_float(data.get("productPrice")) / 100.0,
            "delivery": _to_float(data.get("deliveryPrice")) / 100.0,
            "packing": _to_float(data.get("packingPrice")) / 100.0,
            "discount": _to_float(data.get("discount")) / 100.0,
            "original": _to_float(data.get("originalPrice")) / 100.0,
        }
        breakdown = plan.price_breakdown  # type: ignore[attr-defined]
        if breakdown["discount"] > 0:
            plan.verified_saved = breakdown["discount"]
        extras = []
        if breakdown["delivery"] > 0:
            extras.append(f"配送费 ¥{breakdown['delivery']:.2f}")
        if breakdown["packing"] > 0:
            extras.append(f"打包费 ¥{breakdown['packing']:.2f}")
        if extras:
            plan.notes.append("实付含" + "、".join(extras))
        return plan


# ---------------------------------------------------------------- 文本渲染

def render(plans: list[Plan], people: int, budget: float, store_name: str,
           source_note: str, over_budget: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"我根据 {people} 人、预算 ¥{budget:.0f}、门店「{store_name}」查到以下方案：")
    lines.append("")
    if over_budget:
        labels = ["最接近预算", "次接近预算", "第三接近"]
        lines.append(f"⚠️ 预算 ¥{budget:.0f} 内没有可行组合，以下是最接近的方案：")
        lines.append("")
    else:
        labels = ["最省钱", "均衡饱腹", "丰富尝鲜"]
    for idx, plan in enumerate(plans):
        label = labels[idx] if idx < len(labels) else f"方案 {idx + 1}"
        total = plan.total()
        per = plan.per_capita if plan.per_capita is not None else total / max(1, people)
        lines.append(f"方案 {'ABC'[idx] if idx < 3 else idx + 1}｜{label}")
        for it, q in plan.items:
            unit = f"¥{it.price:.0f}"
            cnt = f" ×{q}" if q > 1 else ""
            lines.append(f"- {it.name}{cnt}（{unit}）")
        lines.append(f"- 预计总价：¥{total:.2f}")
        lines.append(f"- 人均：¥{per:.2f}")
        breakdown = getattr(plan, "price_breakdown", None)
        if breakdown:
            parts = [f"商品 ¥{breakdown['product']:.2f}"]
            if breakdown["delivery"]:
                parts.append(f"配送 ¥{breakdown['delivery']:.2f}")
            if breakdown["packing"]:
                parts.append(f"打包 ¥{breakdown['packing']:.2f}")
            if breakdown["discount"]:
                parts.append(f"优惠 -¥{breakdown['discount']:.2f}")
            lines.append("- 费用拆分：" + " + ".join(parts))
        if total > budget:
            lines.append(f"- ⚠️ 超出预算 ¥{budget:.0f} 约 ¥{total - budget:.2f}")
        saved = plan.verified_saved
        if saved is None:
            saved = sum(it.saved * q for it, q in plan.items)
        if saved and saved > 0:
            lines.append(f"- 优惠/节省：约 ¥{saved:.2f}（相对原价）")
        if plan.reason:
            lines.append(f"- 适合你因为：{plan.reason}")
        for note in plan.notes:
            lines.append(f"- 备注：{note}")
        lines.append("")
    if plans:
        best = plans[0]
        per = best.per_capita if best.per_capita is not None else best.total() / max(1, people)
        if best.total() <= budget:
            lines.append("我的首选：方案 A")
            lines.append(f"原因：总价 ¥{best.total():.2f} 在预算 ¥{budget:.0f} 内，"
                         f"人均 ¥{per:.2f}，最贴合你的约束。")
        else:
            lines.append("我的首选：暂无（预算内无可行方案）")
            lines.append(f"原因：预算 ¥{budget:.0f} 内没有组合可行；最接近的方案 A 总价 "
                         f"¥{best.total():.2f}，仍超 ¥{best.total() - budget:.2f}。"
                         f"建议把预算提到 ¥{best.total():.0f} 以上，或减少人数。")
    lines.append("")
    lines.append("你可以回复「选 A」「换饮料」「预算提高到 X 元」或「查看下单清单」。")
    lines.append("")
    lines.append(source_note)
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def _build_reasons(plans: list[Plan], people: int, budget: float) -> None:
    """给每个方案补上人话理由。"""
    for plan in plans:
        cats = {it.category for it, _ in plan.items}
        n_items = sum(q for _, q in plan.items)
        parts = []
        if "套餐" in cats:
            parts.append("用套餐做主食，单位价格更划算")
        elif "汉堡" in cats:
            parts.append("单点拼配，自由度高")
        if "饮料" in cats:
            parts.append("含饮料，不用另买")
        if "甜品" in cats:
            parts.append("带甜品，体验更完整")
        saved = sum(it.saved * q for it, q in plan.items)
        if saved > 0:
            parts.append(f"相对原价可省约 ¥{saved:.0f}")
        parts.append(f"共 {n_items} 件，人均 ¥{(plan.per_capita or plan.subtotal / max(1, people)):.2f}")
        plan.reason = "；".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="麦门省钱搭子 —— 真实 MCP 点餐方案生成")
    ap.add_argument("--people", type=int, default=2, help="用餐人数")
    ap.add_argument("--budget", type=float, required=True, help="总预算（元）")
    ap.add_argument("--city", help="城市（到店搜索用）")
    ap.add_argument("--keyword", help="位置关键词（到店搜索用）")
    ap.add_argument("--store", help="直接指定 storeCode，跳过门店搜索")
    ap.add_argument("--be-code", help="beCode（得来速/外送场景需要）")
    ap.add_argument("--mode", choices=list(BE_TYPE), default="delivery", help="取餐方式")
    ap.add_argument("--avoid", default="", help="忌口关键词，逗号分隔")
    ap.add_argument("--no-spicy", action="store_true", help="不吃辣")
    ap.add_argument("--no-drink", action="store_true", help="不需要饮料")
    ap.add_argument("--dessert", action="store_true", help="想要甜品")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    be_type = BE_TYPE[args.mode]
    order_type = ORDER_TYPE[args.mode]
    avoid = [w.strip() for w in args.avoid.split(",") if w.strip()]

    try:
        client = McdMCPClient()
    except McdMCPError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    # ---- 1. 定位门店 ----
    store_code, be_code, store_name = args.store, args.be_code, args.store or ""
    if not store_code:
        try:
            if be_type in (2, 6):
                addrs = client.addresses()
                if not addrs:
                    print("[FATAL] 该账号没有可用外送地址，请改用 --mode pickup 并给 --city/--keyword",
                          file=sys.stderr)
                    return 2
                stores = client.delivery_stores(addrs[0]["addressId"], be_type)
            else:
                if not (args.city and args.keyword):
                    print("[FATAL] 到店搜索需要 --city 和 --keyword", file=sys.stderr)
                    return 2
                stores = client.nearby_stores(be_type, search_type=2, city=args.city, keyword=args.keyword)
        except McdMCPError as exc:
            print(f"[FATAL] 门店查询失败：{exc}", file=sys.stderr)
            return 2
        if not stores:
            print("[FATAL] 没有找到可下单门店", file=sys.stderr)
            return 2
        store = stores[0]
        store_code = store.get("storeCode")
        be_code = store.get("beCode")
        store_name = store.get("storeName") or store_code

    # ---- 2. 拉菜单 ----
    try:
        menu = client.meals(store_code, be_type=be_type, be_code=be_code, order_type=order_type)
    except McdMCPError as exc:
        print(f"[FATAL] 菜单查询失败：{exc}", file=sys.stderr)
        return 2

    items = normalize_meals(menu)
    if not items:
        print("[FATAL] 菜单里没有可售商品", file=sys.stderr)
        return 2

    # ---- 3. 生成候选 ----
    planner = MealPlanner(client, items)
    fixed = planner.calibrate(
        store_code, be_type=be_type, be_code=be_code, order_type=order_type, limit=22
    )
    if fixed and not args.json:
        print(f"（已用真实算价校准 {fixed} 个商品的单价；菜单最低价与套餐实际成交价可能不一致）\n")
    plans = planner.generate(
        people=args.people, budget=args.budget, avoid=avoid,
        no_spicy=args.no_spicy, want_drink=not args.no_drink, want_dessert=args.dessert,
        top_n=12,
    )
    if not plans:
        extra = ""
        if avoid:
            extra += f"、忌口 {avoid}"
        if args.no_spicy:
            extra += "、不吃辣"
        print(f"没有满足条件的组合：{args.people} 人、预算 ¥{args.budget:.0f}{extra}"
              "。建议提高预算、减少人数或放宽限制。")
        return 1

    # ---- 4. 真实算价校验（逐个候选，最多 TOP_N 次调用）----
    for plan in plans:
        planner.verify(plan, store_code, be_type=be_type, be_code=be_code, order_type=order_type)
        plan.per_capita = plan.total() / max(1, args.people)

    # ---- 5. 以真实价为唯一准绳筛选 ----
    within = [p for p in plans if p.total() <= args.budget]
    over = sorted([p for p in plans if p.total() > args.budget], key=lambda p: p.total())

    if within:
        # 预算内：优先覆盖不同品类（方案之间要有明显差异），再按价格补齐
        within_sorted = sorted(within, key=lambda p: p.total())
        selected = []
        used_titles: set[str] = set()
        for p in within_sorted:
            if len(selected) >= 3:
                break
            if p.title in used_titles:
                continue
            selected.append(p)
            used_titles.add(p.title)
        for p in within_sorted:
            if len(selected) >= 3:
                break
            if p not in selected:
                selected.append(p)
        over_budget_mode = False
    else:
        # 预算内确实无解：只展示最接近的 2 个，并明确标注超预算
        selected = over[:2]
        over_budget_mode = True

    plans = selected
    _build_reasons(plans, args.people, args.budget)

    source_note = (
        "以上价格与可售状态来自麦当劳 MCP 实时返回"
        f"（门店 {store_code}，{len(items)} 个可售商品），最终以下单页面为准。"
    )

    if args.json:
        print(json.dumps(
            {
                "store": {"storeCode": store_code, "beCode": be_code, "name": store_name},
                "people": args.people,
                "budget": args.budget,
                "itemPoolSize": len(items),
                "plans": [p.to_dict() for p in plans],
                "sourceNote": source_note,
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(plans, args.people, args.budget, store_name, source_note, over_budget_mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
