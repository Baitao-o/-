# -*- coding: utf-8 -*-
"""验证「可视化界面」与 SKILL.md 规范是否真正衔接。

逐条打 SKILL.md 承诺的硬约束，确认网页后端确实执行了这些规则，
而不是只有命令行版才生效。
"""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8808"


def post(payload, timeout=180):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/plan", data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show(title, data):
    print("=" * 66)
    print("检查项:", title)
    if not data.get("ok"):
        print("  ❌ 接口失败:", data.get("error"))
        return None
    if data.get("mode") == "structure":
        if data.get("infeasible"):
            print(f"  配不齐：最低所需 ¥{data.get('minimumNeeded')}  "
                  f"还差 ¥{data.get('gap')}")
            print(f"  提示：{data.get('error')}")
            return {"infeasible": True, "feasible": False,
                    "minimumNeeded": data.get("minimumNeeded"),
                    "gap": data.get("gap")}
        best = data["best"]
        total = best["verifiedTotal"] if best["verifiedTotal"] is not None else best["subtotal"]
        print(f"  可行={data['feasible']}  实付=¥{total:.2f}  剩余=¥{data['remaining']:.2f}")
        for it in best["items"]:
            print(f"    [{it['slotLabel']}] {it['name']} ×{it['quantity']}  ¥{it['unitPrice']:.2f}")
        return {"total": total, "items": best["items"], "feasible": data["feasible"]}
    else:
        print(f"  方案数={len(data['plans'])}")
        for p in data["plans"]:
            total = p["verifiedTotal"] if p["verifiedTotal"] is not None else p["subtotal"]
            print(f"    ¥{total:>7.2f}  " + " + ".join(f"{x['name']}×{x['quantity']}" for x in p["items"]))
        return {"plans": data["plans"]}


def main():
    results = []

    # ---- 1) SKILL 承诺：忌口是硬约束，命中即排除 ----
    base = {"budget": 100, "people": 2, "mode": "pickup",
            "city": "绍兴", "keyword": "上虞",
            "main": 1, "snack": 1, "dessert": 1, "drink": 1}
    r0 = show("① 基线（无忌口）", post(base))
    r1 = show("② 忌口「牛肉」→ 主食不应含牛", post(dict(base, avoid="牛肉")))
    if r1 and r0:
        has_beef = any("牛" in it["name"] for it in r1["items"])
        print(f"  → 忌口生效：{'❌ 仍出现牛肉' if has_beef else '✅ 已排除牛肉'}")
        results.append(("忌口硬约束", not has_beef))

    # ---- 2) SKILL 承诺：忌辣关键词排除 ----
    r2 = show("③ 不吃辣 noSpicy=true", post(dict(base, noSpicy=True)))
    if r2:
        spicy = any(any(w in it["name"] for w in ("辣", "椒")) for it in r2["items"])
        print(f"  → 忌辣生效：{'❌ 仍出现辣味' if spicy else '✅ 无辣味商品'}")
        results.append(("忌辣硬约束", not spicy))

    # ---- 3) SKILL 承诺：份数精确满足 + --per-person 按人数翻倍 ----
    r3 = show("④ 结构 main=1 + perPerson(2人) → 应为 2 份主食", post(dict(base, perPerson=True)))
    if r3:
        main_qty = sum(it["quantity"] for it in r3["items"] if it["slot"] == "main")
        ok = main_qty == 2
        print(f"  → 份数翻倍：主食 {main_qty} 份 {'✅' if ok else '❌ 期望 2 份'}")
        results.append(("perPerson 份数翻倍", ok))

    # ---- 4) SKILL 承诺：份数必须精确，不能拿别的品类顶替 ----
    r4 = show("⑤ 结构 2主食+2小吃（无甜品饮品）", post({
        "budget": 100, "people": 2, "mode": "pickup",
        "city": "绍兴", "keyword": "上虞", "main": 2, "snack": 2,
    }))
    if r4:
        qty = {}
        for it in r4["items"]:
            qty[it["slot"]] = qty.get(it["slot"], 0) + it["quantity"]
        ok = qty.get("main") == 2 and qty.get("snack") == 2 and "dessert" not in qty and "drink" not in qty
        print(f"  → 结构精确：{qty} {'✅' if ok else '❌ 与要求不符'}")
        results.append(("结构精确满足", ok))

    # ---- 5) SKILL 承诺：预算内无解要如实说明，不硬凑 ----
    r5 = show("⑥ 预算 20 元配 2主食+2小吃 → 应明确超预算", post({
        "budget": 20, "people": 2, "mode": "pickup",
        "city": "绍兴", "keyword": "上虞", "main": 2, "snack": 2,
    }))
    if r5:
        ok = (r5["feasible"] is False) and bool(r5.get("minimumNeeded"))
        print(f"  → 如实拒绝：feasible={r5['feasible']} "
              f"最低所需=¥{r5.get('minimumNeeded')} {'✅' if ok else '❌ 未给出最低所需金额'}")
        results.append(("预算无解如实说明", ok))

    # ---- 6) SKILL 承诺：排除非食用周边 ----
    r6 = show("⑦ 结构配餐不应出现贴纸/玩具", post(base))
    if r6:
        junk = [it["name"] for it in r6["items"]
                if any(w in it["name"] for w in ("贴", "卡", "玩偶", "玩具", "盲盒"))]
        print(f"  → 周边过滤：{'❌ 出现 ' + str(junk) if junk else '✅ 无周边商品'}")
        results.append(("非食用周边过滤", not junk))

    print()
    print("=" * 66)
    print("汇总：")
    passed = 0
    for name, ok in results:
        print(("  ✅ " if ok else "  ❌ ") + name)
        passed += 1 if ok else 0
    print(f"\n{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
