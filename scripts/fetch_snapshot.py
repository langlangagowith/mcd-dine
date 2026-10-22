#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓一份「离线快照」给单文件前端用（双击 index.html 时没有后端也能跑）。

快照只含公开菜单/门店/券信息，**不含 token、不含账户隐私**。
价格是菜单现价（与 calculate-price 成交价一致，实测 34.00 对 6800 分），
但离线模式下没有走核价接口，所以前端会把数字标成「估算」。

    python scripts/fetch_snapshot.py --keyword 湖滨银泰 --out data/offline.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dine_service import DineService, unsuitable  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="杭州市")
    ap.add_argument("--keyword", default="湖滨银泰")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "offline.json"))
    args = ap.parse_args()

    svc = DineService()
    stores = svc.find_stores(args.city, args.keyword)
    if not stores:
        print("× 没搜到门店")
        return 1
    store = next((s for s in stores if s.get("businessStatus")), stores[0])
    print(f"→ 门店 {store['storeCode']} {store.get('storeName')}")

    meals = svc.menu(store)
    kcal = svc.nutrition()
    for m in meals:
        from dine_service import _match_kcal
        m.kcal, _ = _match_kcal(m.name, kcal)
        if m.role == "combo":
            m.kcal = None
    coupons = svc.store_coupons(store)
    claimable = svc.claimable_coupons()

    payload = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "麦当劳中国 MCP：query-nearby-stores + query-meals + query-store-coupons",
        "store": {k: v for k, v in store.items() if not k.startswith("_")},
        "menu": [{
            "code": m.code, "name": m.name, "price": m.price, "orig": m.orig_price,
            "role": m.role, "cat": m.category.replace("\n", ""), "tags": m.tags,
            "img": m.image, "kcal": m.kcal, "skip": unsuitable(m), "wo": m.with_order,
        } for m in meals],
        "coupons": [{
            "title": c.get("title"), "couponId": c.get("couponId"),
            "couponCode": c.get("couponCode"), "valid": c.get("tradeDateTime"),
            "products": c.get("products") or [],
        } for c in coupons],
        "claimable": [{
            "title": c.get("couponName"), "label": c.get("label"),
            "image": c.get("couponImage"),
        } for c in claimable],
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    size = os.path.getsize(args.out)
    print(f"✅ {args.out}  {size/1024:.0f} KB · 菜单 {len(meals)} 项 · 券 {len(coupons)} 张 · 可领 {len(claimable)} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
