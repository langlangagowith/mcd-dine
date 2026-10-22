#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录一段「真实调用」证据：找店 → 套餐详情 → 券前/券后核价。

产出 docs/evidence/<日期>-live-calls.md，里面每条结论都挂着 traceId，
评委可以拿去和麦当劳侧日志对照；也方便我们自己回溯"这个价是哪来的"。

    python scripts/rehearse.py --keyword 湖滨银泰
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dine_service import DineService, Item, Meal, order_rows  # noqa: E402

OUT_DIR = os.path.join(ROOT, "docs", "evidence")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="杭州市")
    ap.add_argument("--keyword", default="湖滨银泰")
    ap.add_argument("--people", type=int, default=3)
    args = ap.parse_args()

    svc = DineService()
    os.makedirs(OUT_DIR, exist_ok=True)
    calls: list[dict] = []
    t_start = time.time()

    def snap(fn, *a, **kw):
        """执行一步并只收本次真正发生的调用（缓存命中就不记）。"""
        n = len(svc.cli.trace)
        out = fn(*a, **kw)
        calls.extend(svc.cli.trace[n:])
        return out

    # ① 找店
    stores = snap(svc.find_stores, args.city, args.keyword)
    if not stores:
        print("× 没搜到门店")
        return 1
    store = next((s for s in stores if s.get("businessStatus")), stores[0])
    print(f"① 门店 {store['storeCode']} {store.get('storeName')}")

    # ② 菜单
    meals = snap(svc.menu, store)
    print(f"② 菜单 {len(meals)} 项")

    # ③ 门店券
    coupons = snap(svc.store_coupons, store)
    print(f"③ 券池 {len(coupons)} 张")

    # ④ 挑一个「固定价」券做前后对比；没有就挑第一个带商品的券
    pick, code = None, None
    for c in coupons:
        prods = c.get("products") or []
        if prods and prods[0].get("productCode") in {m.code for m in meals}:
            pick, code = c, prods[0]["productCode"]
            break
    if not pick:
        print("⚠ 门店券池里没有能对上菜单的券，跳过券对比（换家店或先领券）")
        return 2

    meal = next(m for m in meals if m.code == code)

    # ④ 套餐详情：挑一个真套餐（证明"这套里到底有什么"），没有就退回券商品
    combo = next((m for m in meals if m.role == "combo" and not m.with_order), meal)
    detail = snap(svc.meal_detail, store, combo.code)
    parts = []
    for r in (detail or {}).get("rounds") or []:
        for ch in r.get("choices") or []:
            if ch.get("isDefault") == 1:
                parts.append(ch.get("name"))
    print(f"④ 套餐详情 {combo.name} = {' + '.join(parts) or '—'}")

    # ⑥ 券前核价
    qty = args.people
    base_items = [{"productCode": code, "quantity": qty}]
    d0 = snap(svc.price, store, base_items)
    # ⑦ 券后核价（1 份用券 + 其余原价，接口不会替我们拆行）
    line = Item(meal, qty)
    line.coupon = {"couponId": pick["couponId"], "couponCode": pick["couponCode"]}
    d1 = snap(svc.price, store, order_rows([line]))

    p0 = round(float(d0.get("price") or 0) / 100, 2)
    p1 = round(float(d1.get("price") or 0) / 100, 2)
    print(f"⑤ 核价 {p0:.2f} → {p1:.2f}（省 {p0 - p1:.2f}）")

    md = render(store, meals, coupons, pick, meal, combo, parts, qty, p0, p1, d0, d1, calls,
                (time.time() - t_start) * 1000, args)
    stamp = time.strftime("%Y-%m-%d")
    path = os.path.join(OUT_DIR, f"{stamp}-live-calls.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print(f"✅ {path}")

    raw = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "store": store, "coupon": pick, "meal": {"code": meal.code, "name": meal.name},
        "priceBefore": p0, "priceAfter": p1, "calcBefore": d0, "calcAfter": d1,
        "calls": calls,
    }
    with open(os.path.join(OUT_DIR, f"{stamp}-live-calls.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return 0


def render(store, meals, coupons, pick, meal, combo, parts, qty, p0, p1, d0, d1, calls, ms, args) -> str:
    L = []
    L.append("# 真实调用记录（自动生成）\n")
    L.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　·　生成脚本：`scripts/rehearse.py`\n")
    L.append(f"> 服务端：麦当劳中国 MCP `mcp.mcd.cn`　·　总耗时 {ms/1000:.1f}s　·　共 {len(calls)} 次调用\n")
    L.append("\n这份文件里的每个价格、券号、traceId 都是一次真实网络往返的结果，没有手工改过。\n")

    L.append("\n## 0. 场景\n")
    L.append(f"- 门店：**{store.get('storeName')}**（`storeCode={store['storeCode']}`，"
             f"营业 {store.get('businessStartTime')}-{store.get('businessEndTime')}）\n")
    L.append(f"- 城市/位置：{args.city} · {args.keyword}　·　人数：{qty}\n")
    L.append(f"- 菜单在售：{len(meals)} 项　·　门店券池：{len(coupons)} 张\n")

    L.append("\n## 1. 套餐组成（query-meal-detail）\n")
    L.append(f"**{combo.name}**（`code={combo.code}`，菜单价 {combo.price:.2f} 元）")
    L.append(f" = {' + '.join(parts) if parts else '（该商品不是套餐，详情无 round 结构）'}\n")

    L.append("\n## 2. 券前 / 券后（同一份商品，只差一张券）\n")
    L.append(f"商品：**{meal.name}** ×{qty}（`productCode={meal.code}`）\n")
    if parts:
        L.append(f"套餐组成（`query-meal-detail` 默认选项）：{' + '.join(parts)}\n")
    L.append("\n| | 实付 | calculate-price 原始字段 |\n|---|---|---|\n")
    L.append(f"| 券前 | **{p0:.2f} 元** | `price={d0.get('price')}` `discount={d0.get('discount')}` |\n")
    L.append(f"| 券后 | **{p1:.2f} 元** | `price={d1.get('price')}` `discount={d1.get('discount')}` |\n")
    L.append(f"\n省 **{p0 - p1:.2f} 元**。券：**{pick.get('title')}**　"
             f"`couponId={pick.get('couponId')}`　`couponCode={pick.get('couponCode')}`\n")

    L.append("\n## 3. 逐次调用（工具 → traceId）\n")
    L.append("| # | 工具 | 返回码 | 耗时 | traceId |\n|---|---|---|---|---|\n")
    for i, c in enumerate(calls, 1):
        L.append(f"| {i} | `{c['tool']}` | {c.get('code')} | {c.get('ms')}ms | "
                 f"`{c.get('traceId')}` |\n")

    L.append("\n## 4. 怎么复核\n")
    L.append("```bash\n")
    L.append("python src/mcp_client.py call query-nearby-stores "
             f"'{{\"beType\":1,\"searchType\":2,\"city\":\"{args.city}\",\"keyword\":\"{args.keyword}\"}}'\n")
    L.append(f"python src/mcp_client.py call query-meal-detail "
             f"'{{\"storeCode\":\"{store['storeCode']}\",\"orderType\":1,\"beType\":1,\"code\":\"{meal.code}\"}}'\n")
    L.append(f"python src/mcp_client.py call calculate-price "
             f"'{{\"storeCode\":\"{store['storeCode']}\",\"orderType\":1,\"beType\":1,"
             f"\"items\":[{{\"productCode\":\"{meal.code}\",\"quantity\":1,\"couponId\":\"{pick.get('couponId')}\","
             f"\"couponCode\":\"{pick.get('couponCode')}\"}},"
             f"{{\"productCode\":\"{meal.code}\",\"quantity\":{max(qty-1,0)}}}]}}'\n")
    L.append("```\n")
    L.append("\n> 券价会随活动结束/库存变化，复跑时数字可能不同——**同一天同门店同券**应当一致。\n")
    return "".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
