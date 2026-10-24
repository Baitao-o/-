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
    ("套餐", ("套餐", "三件套", "四件套", "分享餐", "分享桶", "全家桶", "单人餐", "双人餐", "四人餐")),
    ("汉堡", ("堡", "汉堡", "巨无霸", "安格斯", "板烧", "麦辣鸡腿")),
    ("甜品", ("冰淇淋", "冰激凌", "甜筒", "圆筒", "新地", "麦旋风", "布丁", "蛋糕", "派")),
    ("饮料", ("可乐", "雪碧", "咖啡", "奶茶", "果汁", "牛奶", "奶昔", "摩卡", "拿铁", "饮品", "矿泉水")),
    ("小食", ("薯条", "薯饼", "鸡块", "鸡翅", "鸡腿", "洋葱圈", "玉米", "沙拉", "小食", "鸡排")),
]

# 菜单分类名 -> 品类（分类名比商品名更可靠，优先使用）
CATEGORY_FROM_SECTION: list[tuple[str, tuple[str, ...]]] = [
    ("饮料", ("饮品", "麦咖啡")),
    ("甜品", ("甜品",)),
    ("小食", ("小食", "炸鸡")),
    ("汉堡", ("汉堡", "巨无霸", "安格斯", "大堡口福", "开心乐园", "卷")),
    ("套餐", ("套餐", "分享", "500大卡")),
]

# 非食用周边（贴纸、卡片、玩具等），结构配餐时应排除
NON_FOOD_WORDS = ("贴纸", "贴", "卡片", "徽章", "玩偶", "玩具", "手办", "摆件", "盲盒", "周边")

# 「去首字」宽松匹配时的弱字符黑名单：
# 这些单字过于泛指，用它做匹配会误命中（如「牛肉」去首字得「肉」，
# 会错误匹配到「鸡肉堡」）。只有具备区分度的单字（如「翅」）才允许单字命中。
WEAK_SINGLE_CHARS = set("肉味酱料块片条杯盒餐品类香辣甜鲜堡饼面饭")

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
        """推导品类。

        优先级：菜单分类名 > 商品名关键词。
        只用商品名判断会误判，例如「纯牛奶(盒装)」曾因含「盒」字被当成套餐、
        「心形薯饼8块」里的「派」类关键词也会串味。
        """
        # 1) 菜单分类名（更可靠；「小食甜品/其他」需再用商品名细分）
        dessert_words = dict(CATEGORY_RULES).get("甜品", ())
        for cname in self.categories:
            if "小食甜品" in cname:
                # 该分类同时含小食与甜品，用商品名区分（复用同一份甜品关键词，避免漏词）
                if any(w in self.name for w in dessert_words):
                    return "甜品"
                return "小食"
            for cat, words in CATEGORY_FROM_SECTION:
                if any(w in cname for w in words):
                    return cat
        # 2) 商品名关键词
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


def match_want(item: "Item", want: Iterable[str]) -> list[str]:
    """返回该商品命中的「想吃」关键词列表。

    与 avoid（硬约束、命中即排除）相反，want 是**软偏好**：
    不做硬性过滤，而是通过提高组合评分、以及在结构模式同品类内优先排序，
    让用户想吃的商品优先出现在方案里。

    匹配分三档（从精确到宽松），因为麦当劳商品名常与口语不一致：
      ① 完整子串：want「鸡翅」⊂ 商品名「麦辣鸡翅」
      ② 去首字：  want「鸡翅」→「翅」⊂ 商品名「薄皮焦香V翅」
      ③ 任意二字片段：want「麦旋风」⊂ 商品名「经典麦旋风」
    品类名也算命中，用户说「想吃甜品」可直接落到甜品类。
    """
    hits: list[str] = []
    for w in want or ():
        if not w:
            continue
        # ① 完整子串 / 品类命中
        if w in item.name or w in item.category:
            hits.append(w)
            continue
        if len(w) < 2:
            continue
        # ② 去首字仍命中（鸡翅 -> 翅）；但弱字符（肉/味/块/片…）不算，
        #    否则「想吃牛肉」会误命中「鸡肉堡单品精选」。
        tail = w[1:]
        if tail in item.name and not (len(tail) == 1 and tail in WEAK_SINGLE_CHARS):
            hits.append(w)
            continue
        # ③ 任意连续二字片段命中
        if any(w[i:i + 2] in item.name for i in range(len(w) - 1)):
            hits.append(w)
    return hits


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

    # ---------- 想吃偏好诊断 ----------

    def diagnose_want(
        self,
        want: Iterable[str] = (),
        avoid: Iterable[str] = (),
        no_spicy: bool = False,
    ) -> dict[str, dict]:
        """诊断每个「想吃」关键词在**本门店菜单**里的可满足性。

        为什么需要：用户说「想吃龙虾」，如果菜单里根本没有，就绝不能提示
        「预算或结构限制，可提高预算再试」——那是误导，用户会白提高预算。
        必须区分三种状态：

        - ``not_found``：菜单里没有任何匹配商品 → 明确告知"本店没有"
        - ``excluded`` ：有匹配商品，但被忌口/忌辣排除 → 告知被什么排除了
        - ``found``    ：有可售匹配商品 → 若最终方案仍未包含，才可能是预算/结构原因
        """
        diagnosis: dict[str, dict] = {}
        for w in want or ():
            if not w:
                continue
            matches = [it for it in self.items if match_want(it, [w])]
            if not matches:
                diagnosis[w] = {
                    "status": "not_found",
                    "reason": f"本店菜单里没有「{w}」相关商品",
                    "candidates": [],
                    "matchCount": 0,
                }
                continue

            blocked_by: list[str] = []
            allowed: list[Item] = []
            for it in matches:
                if match_avoid(it.name, avoid):
                    blocked_by.append("忌口")
                    continue
                if no_spicy and is_spicy(it):
                    blocked_by.append("忌辣")
                    continue
                allowed.append(it)

            if not allowed:
                why = "/".join(dict.fromkeys(blocked_by)) or "硬约束"
                diagnosis[w] = {
                    "status": "excluded",
                    "reason": (f"有 {len(matches)} 个「{w}」相关商品，"
                               f"但都被你的{why}排除了"),
                    "candidates": [it.name for it in matches[:6]],
                    "matchCount": len(matches),
                }
            else:
                allowed.sort(key=lambda i: i.price)
                diagnosis[w] = {
                    "status": "found",
                    "reason": "",
                    "candidates": [it.name for it in allowed[:6]],
                    "matchCount": len(allowed),
                    "cheapest": round(allowed[0].price, 2),
                }
        return diagnosis

    # ---------- 评分 ----------

    @staticmethod
    def score(plan: Plan, people: int, budget: float, want_drink: bool,
              want_dessert: bool, want: Iterable[str] = ()) -> float:
        """加权评分：预算贴合 + 人均合理 + 优惠力度 + 品类完整 + 想吃优先。"""
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

        # 6) 正向偏好「想吃」：命中即显著加分，权重高于价格差异，
        #    确保用户点名想吃的东西真的会优先出现在方案里（而不仅是被提到）。
        if want:
            hit_count = sum(len(match_want(it, want)) for it, _ in plan.items)
            s += min(70.0, hit_count * 45.0)
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
        want: Iterable[str] = (),
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
        sets.sort(key=lambda i: (0 if match_want(i, want) else 1, i.price))
        for s in sets[:120]:
            q = max(1, people)
            if s.price * q <= budget:
                combos.append([(s, q)])

        # --- 策略 2：套餐 + 小食 + 饮料 ---
        snacks = sorted([i for i in pool if i.category == "小食"], key=lambda i: (0 if match_want(i, want) else 1, i.price))[:40]
        drinks = sorted([i for i in pool if i.category == "饮料"], key=lambda i: (0 if match_want(i, want) else 1, i.price))[:40]
        desserts = sorted([i for i in pool if i.category == "甜品"], key=lambda i: (0 if match_want(i, want) else 1, i.price))[:30]
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
        burgers = sorted([i for i in pool if i.category == "汉堡"], key=lambda i: (0 if match_want(i, want) else 1, i.price))[:60]
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
            plan.score = self.score(plan, people, budget, want_drink, want_dessert, want)  # type: ignore[attr-defined]
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

    # ---------- 结构约束模式：按品类份数选最低价 ----------

    # 品类 -> 结构槽位
    SLOT_OF_CATEGORY = {
        "汉堡": "main", "套餐": "main",        # 主食
        "小食": "snack",                        # 小吃
        "甜品": "dessert",                      # 甜品
        "饮料": "drink",                        # 饮品
    }
    SLOT_LABEL = {"main": "主食", "snack": "小吃", "dessert": "甜品", "drink": "饮品"}

    def calibrate_categories(self, store_code: str, be_type: int = 1,
                             be_code: str | None = None, order_type: int | None = None,
                             per_cat: int = 6) -> int:
        """按品类分别校准最便宜的商品单价。

        结构模式下每个品类都要出方案，若只校准整体最便宜的商品，
        小食/甜品/饮品可能拿不到真实价，导致算出的"最低价"是假的最低价。
        """
        buckets: dict[str, list[Item]] = {}
        for it in self.pool:
            buckets.setdefault(it.category, []).append(it)

        targets: list[Item] = []
        for _cat, lst in buckets.items():
            targets.extend(sorted(lst, key=lambda i: i.price)[:per_cat])

        fixed = 0
        for it in targets:
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

    def structure_candidates(
        self,
        structure: dict[str, int],
        budget: float,
        avoid: Iterable[str] = (),
        no_spicy: bool = False,
        want: Iterable[str] = (),
        per_category: int = 5,
        budget_slack: float = 1.15,
        max_candidates: int = 24,
    ) -> tuple[list[Plan], dict]:
        """按「品类 -> 份数」结构求解，返回 (候选组合, 错误信息)。

        错误信息是 **dict** 而非字符串，便于网页端区分「配不齐」的三种原因
        并展示最低所需金额（SKILL 规范要求如实说明，而不是笼统报错）。

        `want`（想吃）作为软偏好在**同品类内优先**：命中商品排到该类候选前面，
        即使它比同类最便宜的略贵 —— 这样既尊重用户偏好，又不破坏「按结构配齐」语义。
        """
        import itertools

        need = {k: int(v) for k, v in (structure or {}).items() if int(v or 0) > 0}
        if not need:
            return [], {"kind": "empty_structure", "message": "没有指定任何品类份数"}

        pools: dict[str, list[Item]] = {k: [] for k in need}
        for it in self.pool:
            slot = self.SLOT_OF_CATEGORY.get(it.category)
            if slot not in pools:
                continue
            if match_avoid(it.name, avoid):
                continue
            if any(w in it.name for w in NON_FOOD_WORDS):   # 贴纸/玩具等周边不参与配餐
                continue
            if no_spicy and is_spicy(it):
                continue
            pools[slot].append(it)

        for slot, lst in pools.items():
            if slot == "main":
                # 主食优先用单点汉堡：套餐本身已含小吃/饮品，
                # 若拿套餐当主食，用户额外要求的小吃/饮品就会重复。
                singles = [i for i in lst if i.category == "汉堡"]
                if len(singles) >= 3:
                    lst = singles
            # 排序：想吃命中的优先，其次才按真实单价升序。
            # 结构模式本质是「求最低价」，但用户点名想吃的东西优先级更高，
            # 因此同类内先排命中商品 —— 若命中品更贵，方案总价会略高，输出里会说明。
            lst.sort(key=lambda i: (0 if match_want(i, want) else 1, i.price))
            pools[slot] = lst
            if not lst:
                return [], {
                    "kind": "missing_category",
                    "message": f"该门店没有可售的「{self.SLOT_LABEL[slot]}」类商品",
                    "slot": slot,
                }

        slots = list(need.keys())
        choice_lists = [pools[s][:per_category] for s in slots]

        est_combos: list[tuple[float, list[tuple[Item, int]]]] = []
        for picks in itertools.product(*choice_lists):
            items = [(it, need[s]) for s, it in zip(slots, picks)]
            est = sum(it.price * q for it, q in items)
            if est > budget * budget_slack:
                continue
            est_combos.append((est, items))

        if not est_combos:
            cheapest = sum((pools[s][0].price * need[s]) for s in slots)
            return [], {
                "kind": "over_budget",
                "message": (f"按该结构的最低商品总价约 ¥{cheapest:.2f}，"
                            f"已超出预算 ¥{budget:.0f}；请提高预算或减少份数"),
                "minimumNeeded": round(cheapest, 2),
                "budget": budget,
                "gap": round(cheapest - budget, 2),
            }

        # 排序：想吃命中的组合要进验证队列，否则「想吃」商品通常更贵，
        # 会在 verify 只取前 12 名时被丢掉，导致明明能配出来却提示「未能包含」。
        # 但也不能全排命中项 —— 否则「其实还有更便宜的非命中方案」会消失，
        # 用户就看不到真实价差。因此两组交替排列。
        est_combos.sort(key=lambda x: x[0])
        if want:
            hit = [x for x in est_combos if any(match_want(it, want) for it, _ in x[1])]
            miss = [x for x in est_combos if not any(match_want(it, want) for it, _ in x[1])]
            merged = []
            for i in range(max(len(hit), len(miss))):
                if i < len(hit):
                    merged.append(hit[i])
                if i < len(miss):
                    merged.append(miss[i])
            est_combos = merged

        plans: list[Plan] = []
        for est, items in est_combos[:max_candidates]:
            key = "|".join(f"{it.code}x{q}" for it, q in items)
            plan = Plan(
                key=key,
                title="结构配齐",
                reason="",
                items=items,
                subtotal=est,
                per_capita=est / max(1, sum(need.values())),
            )
            plans.append(plan)
        return plans, ""

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


def render_structure(plan: Plan, structure: dict[str, int], budget: float,
                     store_name: str, source_note: str) -> str:
    """渲染「按结构配齐、取最低价」的结果。"""
    lines: list[str] = []
    desc = "、".join(
        f"{MealPlanner.SLOT_LABEL[k]} {v} 份" for k, v in structure.items() if v > 0
    )
    lines.append(f"预算 ¥{budget:.0f}，结构要求：{desc}，门店「{store_name}」")
    lines.append("")

    total = plan.total()
    prefer = "已满足想吃" in (plan.reason or "")
    tag = "**偏好优先组合**" if prefer else "**最低价组合**"
    if plan.verified_total is None:
        lines.append(f"（未能取得真实算价，以下为菜单价估算 ¥{plan.subtotal:.2f}）")
    if total <= budget:
        lines.append(f"✅ 符合该结构的{tag}：实付 ¥{total:.2f}，预算内还余 ¥{budget - total:.2f}")
    else:
        lines.append(f"⚠️ 符合该结构的{tag}仍需 ¥{total:.2f}，超出预算 ¥{total - budget:.2f}")
    lines.append("")

    for slot, label in MealPlanner.SLOT_LABEL.items():
        if slot not in structure or structure[slot] <= 0:
            continue
        picked = [
            (it, q) for it, q in plan.items
            if MealPlanner.SLOT_OF_CATEGORY.get(it.category) == slot
        ]
        for it, q in picked:
            lines.append(
                f"- **{label} {q} 份**：{it.name}　¥{it.price:.2f}/份　小计 ¥{it.price * q:.2f}"
            )
    lines.append("")

    breakdown = getattr(plan, "price_breakdown", None)
    if breakdown:
        parts = [f"商品 ¥{breakdown['product']:.2f}"]
        if breakdown["delivery"]:
            parts.append(f"配送 ¥{breakdown['delivery']:.2f}")
        if breakdown["packing"]:
            parts.append(f"打包 ¥{breakdown['packing']:.2f}")
        if breakdown["discount"]:
            parts.append(f"优惠 -¥{breakdown['discount']:.2f}")
        lines.append("费用拆分：" + " + ".join(parts))
    saved = plan.verified_saved
    if saved and saved > 0:
        lines.append(f"共节省：¥{saved:.2f}（相对原价）")
    if plan.reason:
        lines.append("")
        lines.append(f"说明：{plan.reason}")
    for note in plan.notes:
        lines.append(f"备注：{note}")
    lines.append("")
    # 注意：开启「想吃」偏好后，选中的不一定是绝对最低价方案，
    # 因此这里不能笼统写「总价最低」，否则是虚假声明。
    if "已满足想吃" in (plan.reason or ""):
        lines.append("以上为**优先满足你「想吃」偏好**后、预算内可选的方案；"
                     "若只看价格，可去掉偏好再查一次。")
    else:
        lines.append("以上为该门店**当前可售、符合结构、总价最低**的组合；"
                     "如需更便宜可减少份数或提高预算档位对比。")
    lines.append("")
    lines.append(source_note)
    return "\n".join(lines)




def _build_reasons(plans: list[Plan], people: int, budget: float,
                   want: Iterable[str] = ()) -> None:
    """给每个方案补上人话理由（含「想吃」命中情况）。"""
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
        # 正向偏好：告诉用户想吃的有没有出现
        if want:
            hits: list[str] = []
            for it, _q in plan.items:
                hits.extend(match_want(it, want))
            hits = list(dict.fromkeys(hits))
            missed = [w for w in want if w and w not in hits]
            if hits:
                parts.append("已满足想吃：" + "、".join(hits))
            if missed:
                # 只陈述事实，具体原因由 diagnose_want() 的诊断结果说明，
                # 避免菜单里根本没有时误导用户"再提高预算试试"。
                parts.append("本次方案未包含：" + "、".join(missed))
        saved = sum(it.saved * q for it, q in plan.items)
        if saved > 0:
            parts.append(f"相对原价可省约 ¥{saved:.0f}")
        parts.append(f"共 {n_items} 件，人均 ¥{(plan.per_capita or plan.subtotal / max(1, people)):.2f}")
        plan.reason = "；".join(parts)



def _print_want_diagnosis(diagnosis: dict[str, dict]) -> None:
    """打印「想吃」偏好的可满足性诊断（菜单没有 vs 被排除 vs 预算不够）。"""
    for w, d in (diagnosis or {}).items():
        st = d.get("status")
        if st == "not_found":
            print(f"⚠️  本店菜单里没有「{w}」相关商品，这个偏好无法满足"
                  "（不是预算问题，不用再提高预算）。")
        elif st == "excluded":
            print(f"⚠️  {d.get('reason')}")
        elif st == "found":
            cands = "、".join(d.get("candidates") or [])
            print(f"ℹ️  「{w}」本店有 {d.get('matchCount')} 个可选（如 {cands}）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="麦门省钱搭子 —— 真实 MCP 点餐方案生成")
    ap.add_argument("--people", type=int, default=2, help="用餐人数")
    ap.add_argument("--budget", type=float, required=True, help="总预算（元）")
    ap.add_argument("--city", help="城市（到店搜索用）")
    ap.add_argument("--keyword", help="位置关键词（到店搜索用）")
    ap.add_argument("--store", help="直接指定 storeCode，跳过门店搜索")
    ap.add_argument("--be-code", help="beCode（得来速/外送场景需要）")
    ap.add_argument("--mode", choices=list(BE_TYPE), default="delivery", help="取餐方式")
    ap.add_argument("--avoid", default="", help="忌口关键词，逗号分隔（硬约束，命中即排除）")
    ap.add_argument("--want", default="",
                    help="想吃的东西，逗号分隔（软偏好，会优先出现在方案里）"
                         "；可写商品名或品类名，如 '鸡翅' 或 '甜品'")
    ap.add_argument("--no-spicy", action="store_true", help="不吃辣")
    ap.add_argument("--no-drink", action="store_true", help="不需要饮料")
    ap.add_argument("--want-dessert", action="store_true", help="想要甜品（默认模式偏好）")
    # ---- 结构约束模式：用户指定各品类份数，求最低价组合 ----
    ap.add_argument("--main", type=int, default=0, help="主食份数（结构模式）")
    ap.add_argument("--snack", type=int, default=0, help="小吃份数（结构模式）")
    ap.add_argument("--dessert", type=int, default=0, help="甜品份数（结构模式）")
    ap.add_argument("--drink", type=int, default=0, help="饮品份数（结构模式）")
    ap.add_argument("--per-person", action="store_true",
                    help="结构份数按人数翻倍（默认份数是总份数）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    be_type = BE_TYPE[args.mode]
    order_type = ORDER_TYPE[args.mode]
    avoid = [w.strip() for w in args.avoid.split(",") if w.strip()]
    want = [w.strip() for w in args.want.split(",") if w.strip()]

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

    # ===== 结构约束模式（用户指定各品类份数，求最低价）=====
    structure = {
        "main": args.main, "snack": args.snack,
        "dessert": args.dessert, "drink": args.drink,
    }
    structure = {k: v for k, v in structure.items() if v > 0}
    if structure:
        if args.per_person:
            structure = {k: v * max(1, args.people) for k, v in structure.items()}

        fixed_cat = planner.calibrate_categories(
            store_code, be_type=be_type, be_code=be_code, order_type=order_type, per_cat=6
        )
        if fixed_cat and not args.json:
            print(f"（已按品类校准 {fixed_cat} 个商品的真实单价）\n")

        cands, err = planner.structure_candidates(
            structure, args.budget, avoid=avoid, no_spicy=args.no_spicy, want=want,
        )
        if err:
            print(f"无法按该结构出方案：{err.get('message', err)}", file=sys.stderr)
            return 1

        # 对估算最便宜的一批做真实算价，避免「估算最低」≠「实付最低」
        verify_n = min(len(cands), 12)
        for p in cands[:verify_n]:
            planner.verify(p, store_code, be_type=be_type, be_code=be_code, order_type=order_type)
            slots_total = max(1, sum(structure.values()))
            p.per_capita = p.total() / slots_total

        verified = [p for p in cands[:verify_n] if p.verified_total is not None]
        within = [p for p in verified if p.total() <= args.budget]

        def _want_hits(pl):
            return sum(len(match_want(it, want)) for it, _ in pl.items)

        # 想吃优先：预算内若有命中「想吃」的方案，优先选它（即使不是最便宜），
        # 并如实说明它比最便宜方案贵多少，不做无声的取舍。
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
        if best is None:
            if within:
                best = min(within, key=lambda p: p.total())
            elif verified:
                best = min(verified, key=lambda p: p.total())
            else:
                best = cands[0]
        _build_reasons([best], sum(structure.values()), args.budget, want=want)

        src_note = (
            f"以上价格来自麦当劳 MCP 实时返回（门店 {store_code}，{len(items)} 个可售商品，"
            f"已对 {verify_n} 个候选做真实算价），最终以下单页面为准。"
        )
        if args.json:
            print(json.dumps({
                "store": {"storeCode": store_code, "beCode": be_code, "name": store_name},
                "budget": args.budget,
                "structure": structure,
                "mode": "structure",
                "best": best.to_dict(),
                "itemPoolSize": len(items),
                "sourceNote": src_note,
                "wantDiagnosis": planner.diagnose_want(want, avoid=avoid, no_spicy=args.no_spicy),
            }, ensure_ascii=False, indent=2))
        else:
            print(render_structure(best, structure, args.budget, store_name, src_note))
            if want:
                print()
                _print_want_diagnosis(
                    planner.diagnose_want(want, avoid=avoid, no_spicy=args.no_spicy))
        return 0

    # ===== 默认模式：按预算生成 2～3 个策略不同的方案 =====
    fixed = planner.calibrate(
        store_code, be_type=be_type, be_code=be_code, order_type=order_type, limit=22
    )
    if fixed and not args.json:
        print(f"（已用真实算价校准 {fixed} 个商品的单价；菜单最低价与套餐实际成交价可能不一致）\n")
    plans = planner.generate(
        people=args.people, budget=args.budget, avoid=avoid,
        no_spicy=args.no_spicy, want_drink=not args.no_drink, want_dessert=args.want_dessert,
        want=want,
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
    _build_reasons(plans, args.people, args.budget, want=want)

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
                "wantDiagnosis": planner.diagnose_want(want, avoid=avoid, no_spicy=args.no_spicy),
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(plans, args.people, args.budget, store_name, source_note, over_budget_mode))
        if want:
            print()
            _print_want_diagnosis(
                planner.diagnose_want(want, avoid=avoid, no_spicy=args.no_spicy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
