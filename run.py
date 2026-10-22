#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcd-dine 入口。

    python run.py                      # 起本地服务（前端连真实 MCP）
    python run.py demo --people 3 --budget 40 --avoid beef --keyword 湖滨银泰
    python run.py stores --keyword 火车东站
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dine_service import DineError, DineService  # noqa: E402


def cmd_demo(args) -> int:
    svc = DineService()
    req = {
        "people": args.people,
        "budget": args.budget,
        "avoid": [a for a in (args.avoid or "").split(",") if a],
        "scene": args.scene,
        "city": args.city,
        "keyword": args.keyword,
        "want": args.want,
    }
    try:
        res = svc.recommend(req)
    except DineError as e:
        print(f"× {e}")
        if e.hint:
            print(f"  → {e.hint}")
        return 1

    s = res["summary"]
    print(f"门店：{res['store'].get('storeName')}（{res['store'].get('storeCode')}）")
    print(f"菜单 {s['menuItems']} 项 · 忌口排除 {s['excluded']} · 候选 {s['candidates']} 个 · "
          f"核价 {s['priced']} 次 · 券池 {s['couponPool']} 张 · 总调用 {s['calls']} 次 · {s['elapsedMs']}ms")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1)[:8000])
        return 0
    for i, p in enumerate(res["plans"], 1):
        print(f"\n[{i}] {p['title']}")
        for it in p["items"]:
            print(f"    {it['name']} ×{it['qty']}  {it['unitPrice']:.2f} → {it['subtotal']:.2f}"
                  + (f"   [{it['note']}]" if it["note"] else ""))
        print(f"    合计 {p['total']:.2f} 元 / 人均 {p['perPerson']:.2f} · 用掉预算 "
              f"{p['useRatio']*100:.1f}% · 券省 {p['couponSaving']:.2f} · 人均 {p['kcalPerPerson'] or '-'} kcal")
        for r in p["reasons"]:
            print(f"      · {r['text']}  「{r['badge']}」")
    return 0


def cmd_stores(args) -> int:
    svc = DineService()
    for s in svc.find_stores(args.city, args.keyword):
        print(f"{s['storeCode']} {'营业' if s.get('businessStatus') else '休息'} "
              f"{s.get('businessStartTime')}-{s.get('businessEndTime')} {s.get('storeName')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="mcd-dine · 麦当劳配餐器")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("demo", help="命令行跑一次配餐")
    d.add_argument("--people", type=int, default=3)
    d.add_argument("--budget", type=float, default=40)
    d.add_argument("--avoid", default="")
    d.add_argument("--scene", default="pickup", choices=["pickup", "delivery"])
    d.add_argument("--city", default="杭州市")
    d.add_argument("--keyword", default="湖滨银泰")
    d.add_argument("--want", default="")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("stores", help="找店")
    s.add_argument("--city", default="杭州市")
    s.add_argument("--keyword", default="湖滨银泰")
    s.set_defaults(func=cmd_stores)

    args = ap.parse_args()
    if not args.cmd:
        from serve import main as serve_main
        return serve_main()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
