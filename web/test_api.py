# -*- coding: utf-8 -*-
"""可视化界面 API 冒烟测试（正确发 UTF-8 请求）。"""
import io
import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE = "http://127.0.0.1:8808"


def post(path, payload, timeout=180):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    cases = [
        ("结构 1/1/1/1 · 预算100", {
            "budget": 100, "people": 2, "mode": "pickup",
            "city": "绍兴", "keyword": "上虞",
            "main": 1, "snack": 1, "dessert": 1, "drink": 1,
        }),
        ("结构 2/1/0/2 · 预算100", {
            "budget": 100, "people": 2, "mode": "pickup",
            "city": "绍兴", "keyword": "上虞",
            "main": 2, "snack": 1, "drink": 2,
        }),
        ("结构全 0 → 预算模式 · 预算130", {
            "budget": 130, "people": 2, "mode": "pickup",
            "city": "绍兴", "keyword": "上虞",
        }),
    ]

    for name, payload in cases:
        print("=" * 70)
        print("用例:", name)
        t0 = time.time()
        try:
            data = post("/api/plan", payload)
        except urllib.error.HTTPError as exc:
            print("  HTTP", exc.code, exc.read().decode("utf-8", "replace")[:300])
            continue
        except Exception as exc:  # noqa: BLE001
            print("  失败:", exc)
            continue
        dt = time.time() - t0
        print(f"  耗时 {dt:.1f}s  ok={data.get('ok')}  mode={data.get('mode')}")

        if not data.get("ok"):
            print("  错误:", data.get("error"))
            continue

        print("  门店:", data["store"]["name"], "|", data["store"]["storeCode"],
              "| 商品池:", data.get("itemPoolSize"))
        if data.get("mode") == "structure":
            best = data["best"]
            print(f"  结构: {data['structure']}  可行={data['feasible']}")
            for it in best["items"]:
                print(f"    {it['slotLabel']:<4} {it['name'][:22]:<24} "
                      f"×{it['quantity']}  {it['unitPrice']:>6.2f}  小计 {it['subtotal']:>7.2f}")
            total = best["verifiedTotal"] if best["verifiedTotal"] is not None else best["subtotal"]
            print(f"  实付总价: ¥{total:.2f}   剩余预算: ¥{data['remaining']:.2f}")
            if best.get("breakdown"):
                print("  费用拆分:", best["breakdown"])
        else:
            for i, p in enumerate(data["plans"]):
                total = p["verifiedTotal"] if p["verifiedTotal"] is not None else p["subtotal"]
                print(f"    方案 {'ABC'[i]} [{p['title']:<6}] ¥{total:>7.2f}  "
                      + " + ".join(f"{x['name'][:14]}×{x['quantity']}" for x in p["items"]))

    print("=" * 70)
    print("测试完成")


if __name__ == "__main__":
    main()
