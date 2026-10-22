#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 web/app.html + data/offline.json 拼成仓库根目录的 index.html。

单文件的好处：双击就能跑（离线用内嵌真实快照），不需要 node、不需要 npm、不需要起服务。
    python scripts/build_web.py
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "web", "app.html")
SNAP = os.path.join(ROOT, "data", "offline.json")
OUT = os.path.join(ROOT, "index.html")


def main() -> int:
    html = open(APP, encoding="utf-8").read()
    if os.path.isfile(SNAP):
        snap = json.load(open(SNAP, encoding="utf-8"))
        blob = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
        # 防止把 </script> 写坏
        blob = blob.replace("</", "<\\/")
        note = f"内嵌快照 {snap.get('generatedAt')} / {len(snap.get('menu') or [])} 项"
    else:
        blob = "null"
        note = "没有快照（离线模式不可用）"
    if "__SNAPSHOT__" not in html:
        print("× app.html 里没有 __SNAPSHOT__ 占位符")
        return 1
    out = html.replace("__SNAPSHOT__", blob)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"✅ {OUT}  {os.path.getsize(OUT)/1024:.0f} KB · {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
