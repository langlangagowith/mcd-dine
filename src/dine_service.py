#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配餐内核 mcd-dine。

一句话：给定「人数 / 人均预算 / 忌口 / 到店或外送」，用麦当劳 MCP 的真实数据
枚举出一桌搭配，对 Top-K 方案逐个 calculate-price 核价，最后把"为什么"写进结果。

设计纪律（M-CODE 时代的教训）：
  - MCP 接口 600 次/分钟限流，绝不让模型拿 35 个工具挨个试错；
  - 组合在**本地**枚举（万级候选，零网络），只对 Top-K 调 calculate-price；
  - 每次调用都记 trace（工具名 / traceId / 耗时），结果里带证据链；
  - 忌口/热量这类"看起来能算"的东西，宁可标注依据等级，也不假装精确。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Iterable

import mcp_client
from mcp_client import MCPClient, ToolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_PATH = os.path.join(DATA_DIR, "detail_cache.json")

# --- 忌口词表 -------------------------------------------------------------
# 这是"名称/组成关键词"判定，不是配料表；结果里会如实标注判定来源。
AVOID_LEXICON: dict[str, list[str]] = {
    "beef":     ["牛", "安格斯", "巨无霸", "吉士", "双层堡"],
    "chicken":  ["鸡", "板烧", "麦乐", "脆皮", "鸡腿", "鸡翅"],
    "pork":     ["猪", "火腿", "培根", "扒"],
    "fish":     ["鱼", "鳕", "虾"],
    "spicy":    ["辣", "椒", "藤", "川式", "咖喱"],
    "egg":      ["蛋"],
    "dairy":    ["芝士", "奶", "冰淇淋", "圣代", "甜筒", "麦旋风", "新地"],
    "caffeine": ["咖啡", "美式", "拿铁", "奶铁", "麦咖啡"],
}

AVOID_LABEL = {
    "beef": "不吃牛肉", "chicken": "不吃鸡肉", "pork": "不吃猪肉",
    "fish": "不吃鱼虾", "spicy": "不吃辣", "egg": "不吃蛋",
    "dairy": "不吃乳制品", "caffeine": "不要咖啡因",
}

DRINK_HINTS = ["可乐", "雪碧", "芬达", "茶", "汁", "咖啡", "美式", "拿铁", "奶铁",
               "矿泉水", "饮用水", "椰", "柠檬", "苏打"]
DESSERT_HINTS = ["派", "冰淇淋", "新地", "麦旋风", "圣代", "甜筒", "奥利奥", "珍珠",
                 "布丁", "蛋糕", "曲奇", "圆筒"]
SIDE_HINTS = ["薯", "鸡块", "鸡翅", "鸡排", "鸡柳", "玉米", "沙拉", "薯饼", "芝士条",
              "洋葱圈", "麦乐鸡", "蘸酱", "小食", "鸡盒", "鸡米花"]
COMBO_HINTS = ["套", "组合", "分享", "双人", "三人", "四人", "全家", "拼盘", "桶", "随心配"]

# 不适合进「一桌搭配」的东西：周边玩具、学生专享、儿童餐、单卖的蘸酱
EXCLUDE_NAME_HINTS = ["周边", "丝巾", "帽", "捶", "挠", "玩具", "公仔", "盲盒",
                      "礼盒", "百宝袋", "学生", "儿童", "亲子", "派对", "会员卡"]
EXCLUDE_CATS = ["开心乐园"]


def _cat(cat: str) -> str:
    return (cat or "").replace("\n", "").replace(" ", "")


def classify(cat: str, name: str, tags: Iterable[str] = ()) -> str:
    """combo / main / side / drink / dessert"""
    c, n = _cat(cat), name or ""
    tags = list(tags or [])
    if "套餐" in tags or "套餐" in n or any(h in n for h in COMBO_HINTS):
        return "combo"
    if "饮品" in c or "麦咖啡" in c:
        return "drink"
    if "小食" in c or "炸鸡" in c:
        return "side"
    if "甜品" in c:
        return "dessert"
    if any(h in n for h in DRINK_HINTS):
        return "drink"
    if any(h in n for h in DESSERT_HINTS):
        return "dessert"
    if any(h in n for h in SIDE_HINTS):
        return "side"
    if "汉堡" in c or "堡" in c or "卷" in n:
        return "main"
    return "main"


def avoid_hits(text: str, avoid: Iterable[str]) -> list[str]:
    return [a for a in avoid if any(k in text for k in AVOID_LEXICON.get(a, []))]


def unsuitable(meal: "Meal") -> bool:
    """周边/玩具/学生专享/儿童餐/单卖蘸酱——不是一顿饭，不该出现在配餐结果里。"""
    n, c = meal.name, _cat(meal.category)
    if any(h in n for h in EXCLUDE_NAME_HINTS):
        return True
    if c in EXCLUDE_CATS:
        return True
    if "周边" in (meal.tags or []):
        return True
    if "酱" in n and meal.price <= 5:
        return True
    return False


# --- 数据模型 -------------------------------------------------------------
@dataclass
class Meal:
    code: str
    name: str
    price: float          # 元，菜单现价（实测 = calculate-price 成交价）
    orig_price: float
    role: str
    category: str
    tags: list[str] = field(default_factory=list)
    image: str = ""
    kcal: float | None = None
    kcal_name: str = ""
    discount_type: str | None = None
    with_order: bool = False      # 随单购价（要加购麦金卡），默认不进候选


@dataclass
class Item:
    meal: Meal
    qty: int
    note: str = ""
    coupon: dict | None = None

    @property
    def subtotal(self) -> float:
        return round(self.meal.price * self.qty, 2)


@dataclass
class Plan:
    items: list[Item]
    template: str                 # combo / alacarte
    score: float = 0.0
    signature: tuple = ()
    priced_total: float | None = None
    promo_discount: float = 0.0
    coupon_total: float | None = None
    coupon_saving: float = 0.0
    used_coupons: list[dict] = field(default_factory=list)
    topup: dict | None = None
    take_way: list[dict] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)

    @property
    def local_total(self) -> float:
        return round(sum(i.subtotal for i in self.items), 2)

    @property
    def total(self) -> float:
        if self.coupon_total is not None:
            return self.coupon_total
        if self.priced_total is not None:
            return self.priced_total
        return self.local_total

    def qty_by_role(self, role: str) -> int:
        return sum(i.qty for i in self.items if i.meal.role == role)

    def codes(self) -> set[str]:
        return {i.meal.code for i in self.items}

    def core(self) -> frozenset:
        """主餐 + 小食 的品类集合（不看份数、不含饮料）：用来去重“只换杯饮料/只换份数”的假差异。"""
        return frozenset(i.meal.code for i in self.items
                         if i.meal.role in ("combo", "main", "side", "dessert"))


class DineError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# --- 服务 -----------------------------------------------------------------
class DineService:
    def __init__(self, client: MCPClient | None = None):
        self.cli = client or mcp_client.connect()
        self._detail_cache = _load_cache()
        self._menu_index: dict[str, float] = {}
        self._ttl: dict[str, tuple[float, object]] = {}   # key -> (过期时刻, 值)

    def _cached(self, key: str, ttl: int, fn):
        hit = self._ttl.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        val = fn()
        self._ttl[key] = (time.time() + ttl, val)
        return val

    # -- 取数据 ------------------------------------------------------------
    def find_stores(self, city: str, keyword: str, be_type: int = 1) -> list[dict]:
        sc = self.cli.call("query-nearby-stores", {
            "beType": be_type, "searchType": 2, "city": city or "杭州市", "keyword": keyword,
        })
        return sc.get("data") or []

    def menu(self, store: dict, order_type: int = 1, be_type: int = 1) -> list[Meal]:
        key = f"menu:{store['storeCode']}:{order_type}"
        if key in self._ttl and self._ttl[key][0] > time.time():
            return list(self._ttl[key][1])  # type: ignore[arg-type]
        sc = self.cli.call("query-meals", self._args(store, order_type, be_type))
        d = sc.get("data") or {}
        raw = d.get("meals") or {}
        cat_of: dict[str, str] = {}
        tags_of: dict[str, list[str]] = {}
        for c in d.get("categories") or []:
            cname = c.get("name") or ""
            for m in c.get("meals") or []:
                code = str(m.get("code") or "")
                if not code:
                    continue
                if code not in cat_of or _cat(cat_of.get(code, "")) in ("人气热卖", "其他", ""):
                    cat_of[code] = cname
                tags_of.setdefault(code, [])
                for t in m.get("tags") or []:
                    if t and t not in tags_of[code]:
                        tags_of[code].append(t)
        meals: list[Meal] = []
        for code, v in raw.items():
            name = (v.get("name") or "").strip()
            if not name:
                continue
            try:
                price = float(v.get("currentPrice") or 0)
            except (TypeError, ValueError):
                continue
            try:
                orig = float(v.get("originalPrice") or price)
            except (TypeError, ValueError):
                orig = price
            cat = cat_of.get(code, "")
            tags = tags_of.get(code, [])
            dtype = v.get("discountType")
            meals.append(Meal(
                code=code, name=name, price=price, orig_price=orig,
                role=classify(cat, name, tags), category=cat, tags=tags,
                image=v.get("image") or "", discount_type=dtype,
                with_order=bool(v.get("withOrder")) or (dtype or "").find("随单购") >= 0,
            ))
        for m in meals:
            if unsuitable(m):
                m.role = "skip"
        self._menu_index = {m.code: m.price for m in meals}
        self._ttl[key] = (time.time() + 90, meals)
        return meals

    def store_coupons(self, store: dict, order_type: int = 1, be_type: int = 1) -> list[dict]:
        key = f"coupons:{store['storeCode']}:{order_type}"

        def fetch():
            try:
                return self.cli.call("query-store-coupons", self._args(store, order_type, be_type)).get("data") or []
            except ToolError:
                return []
        return self._cached(key, 60, fetch)

    def claimable_coupons(self) -> list[dict]:
        def fetch():
            try:
                return self.cli.call("available-coupons", {}).get("data") or []
            except ToolError:
                return []
        return self._cached("claimable", 120, fetch)

    def nutrition(self) -> list[tuple[str, float]]:
        """营养成分表：MCP 返回的是 `[160]{productName,...}:` + CSV 行，不是 JSON。"""
        def fetch():
            try:
                return _parse_nutrition(self.cli.call("list-nutrition-foods", {}).get("data"))
            except ToolError:
                return []
        return self._cached("nutrition", 600, fetch)

    def meal_detail(self, store: dict, code: str, order_type: int = 1, be_type: int = 1) -> dict | None:
        key = f"{store['storeCode']}:{order_type}:{code}"
        if key in self._detail_cache:
            return self._detail_cache[key]
        try:
            sc = self.cli.call("query-meal-detail", {**self._args(store, order_type, be_type), "code": code})
        except ToolError:
            return None
        d = sc.get("data") or {}
        self._detail_cache[key] = d
        _save_cache(self._detail_cache)
        return d

    # -- 核价 / 建单 -------------------------------------------------------
    def price(self, store: dict, items: list[dict], order_type: int = 1, be_type: int = 1) -> dict:
        sc = self.cli.call("calculate-price", {**self._args(store, order_type, be_type), "items": items})
        return sc.get("data") or {}

    def create_order(self, store: dict, plan: Plan, take_way: str,
                     order_type: int = 1, be_type: int = 1, remark: str = "") -> dict:
        payload = {**self._args(store, order_type, be_type), "items": order_rows(plan.items)}
        if order_type == 1:
            payload["takeWayCode"] = take_way
        if remark:
            payload["remark"] = remark[:50]
        return self.cli.call("create-order", payload).get("data") or {}

    def claim_coupons(self) -> dict:
        try:
            sc = self.cli.call("auto-bind-coupons", {})
        except ToolError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "data": sc.get("data")}

    # -- 主流程 ------------------------------------------------------------
    def recommend(self, req: dict) -> dict:
        t0 = time.time()
        trace_start = len(self.cli.trace)
        people = max(1, min(8, int(req.get("people") or 1)))
        budget = max(5.0, float(req.get("budget") or 30))
        avoid = [a for a in (req.get("avoid") or []) if a in AVOID_LEXICON]
        scene = req.get("scene") or "pickup"
        city = (req.get("city") or "杭州市").strip()
        keyword = (req.get("keyword") or "").strip()
        want = (req.get("want") or "").strip()
        order_type = 1 if scene == "pickup" else 2
        be_type = 1 if scene == "pickup" else 2

        store = self._resolve_store(req, scene, city, keyword)

        # ② 菜单 + 营养 + 券
        meals = self.menu(store, order_type, be_type)
        if not meals:
            raise DineError("这家门店暂时取不到菜单", "换一家门店试试")
        kcal_table = self.nutrition()
        for m in meals:
            m.kcal, m.kcal_name = _match_kcal(m.name, kcal_table)
            if m.role == "combo":
                m.kcal = None   # 套餐组成未展开，用主堡热量冒充整份会低估，宁可不报
        coupons = self.store_coupons(store, order_type, be_type)

        # ③ 忌口过滤
        excluded, kept, verified = self._filter_avoid(meals, avoid, budget, store, order_type, be_type)

        # ④ 本地枚举 + 预排序
        plans = list(_enumerate(kept, people, budget))
        if not plans:
            raise DineError(
                f"没有能在 {people * budget:.0f} 元内配出的组合（{people} 人）",
                "把人均预算调高 5~10 元，或减少一个人")
        coupon_codes = {str(p_.get("productCode")) for c in coupons for p_ in (c.get("products") or [])}
        for p in plans:
            p.score = _score(p, people, budget, want, priced=False, coupon_codes=coupon_codes)
        plans.sort(key=lambda p: p.score, reverse=True)
        picked = _diverse(plans, 6)

        # ⑤ 核价 Top-6（本地枚举 2000+ 组合，只核这 6 个）
        pricing_calls = 0
        for p in picked:
            try:
                data = self.price(store, [{"productCode": i.meal.code, "quantity": i.qty} for i in p.items],
                                  order_type, be_type)
            except ToolError as e:
                msg = str(e)
                if "营业" in msg or "关闭" in msg:
                    raise DineError("门店刚好打烊了，无法核价", "换一家 24 小时店再试") from e
                continue
            pricing_calls += 1
            p.priced_total = round(float(data.get("price") or 0) / 100, 2)
            p.promo_discount = round(float(data.get("discount") or 0) / 100, 2)
            p.take_way = data.get("takeWayList") or []
            p.trace_ids.append(self.cli.trace[-1]["traceId"])

        priced = [p for p in picked if p.priced_total is not None]
        if not priced:
            raise DineError("核价接口没返回结果", "稍后重试；若持续失败请检查门店状态")

        # ⑥ 券优化：只对"这桌真含券可用商品"的候选做券后复核（不浪费调用）
        for p in priced:
            if not (p.codes() & coupon_codes):
                continue
            applied, best = self._try_coupons(store, p, coupons, order_type, be_type)
            if best is not None:
                p.coupon_total = best["total"]
                p.coupon_saving = best["saving"]
                p.used_coupons = applied
                p.trace_ids.append(self.cli.trace[-1]["traceId"])

        # ⑥b 券省下来的钱别浪费：预算还有空间就加一份共享小食，加完再核一次价
        for p in priced:
            if p.coupon_saving < 6:
                continue
            gap = people * budget - p.total
            if gap < 12:
                continue
            cand = self._topup(kept, p, gap)
            if not cand:
                continue
            rows = order_rows(p.items) + [{"productCode": cand.code, "quantity": 1}]
            try:
                data = self.price(store, rows, order_type, be_type)
            except ToolError:
                continue
            total = round(float(data.get("price") or 0) / 100, 2)
            if total <= people * budget and total > p.total:
                before = round(p.total, 2)
                p.items.append(Item(cand, 1, note="用券省下的钱加购"))
                p.coupon_total = total
                p.topup = {"name": cand.name, "price": cand.price, "before": before}
                p.trace_ids.append(self.cli.trace[-1]["traceId"])

        # ⑦ 终排 + 讲理由
        for p in priced:
            p.score = _score(p, people, budget, want, priced=True, coupon_codes=coupon_codes)
        priced.sort(key=lambda p: p.score, reverse=True)
        final = priced[:6]
        for p in final:
            _explain(self, p, store, people, budget, len(excluded), order_type, be_type, coupons)

        claimable = self.claimable_coupons()
        return {
            "ok": True,
            "request": {"people": people, "budget": budget, "avoid": avoid, "scene": scene,
                        "city": city, "keyword": keyword, "want": want},
            "store": {k: v for k, v in store.items() if not k.startswith("_")},
            "summary": {
                "menuItems": len(meals),
                "excluded": len(excluded),
                "verifiedDetails": verified,
                "candidates": len(plans),
                "priced": pricing_calls,
                "couponPool": len(coupons),
                "elapsedMs": int((time.time() - t0) * 1000),
                "calls": len(self.cli.trace) - trace_start,
            },
            "plans": [_plan_json(p, people, budget) for p in final],
            "excluded": excluded[:8],
            "coupons": {
                "store": [_coupon_json(c) for c in coupons[:8]],
                "claimable": [_claimable_json(c) for c in claimable[:6]],
            },
            "trace": [t for t in self.cli.trace[trace_start:] if t["tool"] != "initialize"],
        }

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _args(store: dict, order_type: int, be_type: int) -> dict:
        """按官方规则拼参数：到店自取(orderType=1) 不传 beCode；其余必须传。"""
        a = {"storeCode": store["storeCode"], "orderType": order_type, "beType": be_type}
        be_code = store.get("beCode")
        if be_code and (order_type == 2 or be_type in (2, 5, 6)):
            a["beCode"] = be_code
        return a

    def _resolve_store(self, req: dict, scene: str, city: str, keyword: str) -> dict:
        if req.get("storeCode"):
            return {"storeCode": str(req["storeCode"]),
                    "storeName": req.get("storeName") or "",
                    "businessStatus": True}
        if scene == "pickup":
            if not keyword:
                raise DineError("请先填一个位置（地铁站 / 商圈 / 路名）", "例：火车东站、湖滨银泰、延安路")
            stores = self.find_stores(city, keyword, 1)
            if not stores:
                raise DineError(f"「{city} {keyword}」附近没搜到门店", "换个更具体的地标再试")
            store = next((s for s in stores if s.get("businessStatus")), stores[0])
            store["alternatives"] = [
                {"storeCode": s["storeCode"], "storeName": s.get("storeName"),
                 "businessStatus": s.get("businessStatus"),
                 "business": f"{s.get('businessStartTime')}-{s.get('businessEndTime')}"}
                for s in stores[:5]
            ]
            if store.get("businessStatus") is False:
                alt = [s for s in stores if s.get("businessStatus")]
                raise DineError(
                    f"「{store.get('storeName') or store['storeCode']}」此刻不在营业时间"
                    f"（{store.get('businessStartTime')}-{store.get('businessEndTime')}）",
                    ("附近还在营业：" + "、".join(f"{s['storeName']}({s['storeCode']})" for s in alt[:3]))
                    if alt else "换一家 24 小时店（例：杭州朝晖 3330016），或营业时段再来")
            return store

        # 外送
        addrs = []
        try:
            addrs = (self.cli.call("delivery-query-addresses", {}).get("data") or {}).get("addresses") or []
        except ToolError:
            addrs = []
        if not addrs:
            raise DineError("外送模式需要先在麦当劳 App/小程序里存一个配送地址",
                            "本账号目前没有可用地址：可先用到店自取，或去 App 添加地址后回来")
        addr = addrs[0]
        ds = self.cli.call("delivery-query-stores",
                           {"addressId": addr.get("addressId"), "beType": 2}).get("data") or []
        if not ds:
            raise DineError("该地址暂无可配送门店", "换个地址试试")
        store = ds[0]
        store["_address"] = addr
        return store

    def _filter_avoid(self, meals, avoid, budget, store, order_type, be_type):
        excluded: list[dict] = []
        kept: list[Meal] = []
        if not avoid:
            return excluded, [m for m in meals if not m.with_order], 0
        need_detail: list[Meal] = []
        for m in meals:
            hits = avoid_hits(m.name + " " + " ".join(m.tags), avoid)
            if hits:
                excluded.append({"name": m.name, "by": hits, "src": "名称/标签"})
                continue
            if m.with_order:
                continue
            kept.append(m)
            if m.role == "combo" and not _has_meat_hint(m.name):
                need_detail.append(m)
        need_detail.sort(key=lambda m: abs(m.price - budget))
        verified = 0
        for m in need_detail[:6]:
            d = self.meal_detail(store, m.code, order_type, be_type)
            if not d:
                continue
            verified += 1
            hits = avoid_hits(_detail_text(d), avoid)
            if hits:
                kept = [k for k in kept if k.code != m.code]
                excluded.append({"name": m.name, "by": hits, "src": "套餐组成"})
        return excluded, kept, verified

    @staticmethod
    def _topup(meals: list[Meal], plan: Plan, gap: float) -> Meal | None:
        """预算还有 gap 元没花：挑一份最能把预算用满的共享小食/甜品。"""
        have = plan.codes()
        cands = [m for m in meals
                 if m.role in ("side", "dessert", "drink") and m.code not in have
                 and not m.with_order and m.role != "skip" and m.price <= gap]
        if not cands:
            return None
        rank = {"side": 0, "dessert": 1, "drink": 2}
        cands.sort(key=lambda m: (rank.get(m.role, 3), -m.price))
        return cands[0]

    def _try_coupons(self, store, plan: Plan, coupons: list[dict], order_type: int, be_type: int):
        codes = plan.codes()
        cands = []
        for c in coupons:
            hits = [p.get("productCode") for p in (c.get("products") or [])
                    if p.get("productCode") in codes]
            if hits:
                cands.append((c, hits[0]))
        if not cands:
            return [], None
        price_of = {i.meal.code: i.meal.price for i in plan.items}
        cands.sort(key=lambda x: price_of.get(x[1], 0), reverse=True)
        coupon, code = cands[0]
        touched = []
        for i in plan.items:
            if i.meal.code == code:
                i.coupon = {"couponId": coupon.get("couponId"), "couponCode": coupon.get("couponCode")}
                i.note = f"券：{coupon.get('title')}"
                touched.append(i)
        items = order_rows(plan.items)
        try:
            data = self.price(store, items, order_type, be_type)
        except ToolError:
            for i in touched:      # 券没用上就别留在单里，否则建单会带上无效券
                i.coupon = None
                i.note = ""
            return [], None
        total = round(float(data.get("price") or 0) / 100, 2)
        base = plan.priced_total or plan.local_total
        saving = round(base - total, 2)
        if saving <= 0.005:
            for i in touched:
                i.coupon = None
                i.note = ""
            return [], None
        return [{"title": coupon.get("title"), "couponId": coupon.get("couponId"),
                 "couponCode": coupon.get("couponCode"), "product": code,
                 "productName": next((p.get("productName") for p in (coupon.get("products") or [])
                                      if p.get("productCode") == code), code),
                 "saving": saving}], {"total": total, "saving": saving}


def order_rows(items: list["Item"]) -> list[dict]:
    """把方案变成 calculate-price / create-order 的 items。

    ⚠ 实测坑：某行带 couponId 时，接口会把这**整行**都按券价算
    （3 份 22.5 元的汉堡挂 11.9 元券 → 返回 11.90 元），所以必须拆：
    1 份带券 + 其余同款不带券，价格才等于真实下单金额。
    """
    rows: list[dict] = []
    for i in items:
        if i.coupon and i.qty > 1:
            rows.append({"productCode": i.meal.code, "quantity": 1,
                         "couponId": i.coupon.get("couponId"),
                         "couponCode": i.coupon.get("couponCode")})
            rows.append({"productCode": i.meal.code, "quantity": i.qty - 1})
        elif i.coupon:
            rows.append({"productCode": i.meal.code, "quantity": i.qty,
                         "couponId": i.coupon.get("couponId"),
                         "couponCode": i.coupon.get("couponCode")})
        else:
            rows.append({"productCode": i.meal.code, "quantity": i.qty})
    return rows


# --- 枚举 -----------------------------------------------------------------
CAP = 1.0     # 预算硬上限：宁可少几块钱，也不超一分


def _enumerate(meals: list[Meal], people: int, budget: float) -> Iterable[Plan]:
    T = people * budget
    usable = [m for m in meals if not m.with_order and m.role != "skip"]
    combos = sorted([m for m in usable if m.role == "combo" and m.price <= budget * 1.15],
                    key=lambda m: m.price)
    mains = sorted([m for m in usable if m.role == "main" and 0.25 * budget <= m.price <= 0.95 * budget],
                   key=lambda m: m.price)
    sides = sorted([m for m in usable if m.role in ("side", "dessert") and 0.05 * budget <= m.price <= 0.65 * budget],
                   key=lambda m: m.price)
    drinks = sorted([m for m in usable if m.role == "drink" and m.price <= 0.45 * budget],
                    key=lambda m: m.price)

    # —— 族 A：每人一份套餐（可 1~2 种，预算有余则加一份共享小食）
    for i, c in enumerate(combos):
        if c.price * people > T * CAP:
            break
        yield _mk([Item(c, people)], "combo")
        if people >= 2 and c.price * people < T * 0.9:
            for s in sides[:6]:
                if c.price * people + s.price <= T * CAP:
                    yield _mk([Item(c, people), Item(s, 1)], "combo")
        for c2 in combos[i + 1:]:
            if (c.price + c2.price) * people > T * 2.1:
                break
            for k in range(1, people):
                if k * c.price + (people - k) * c2.price > T * CAP:
                    continue
                yield _mk([Item(c, k), Item(c2, people - k)], "combo")

    # —— 族 B：单点（N 份主食 + 共享小食 + N 杯饮料，各最多 2 种）
    extras: list[tuple[float, list[tuple[Meal, int]]]] = []
    side_opts: list[list[tuple[Meal, int]]] = [[]]
    for i, s in enumerate(sides):
        side_opts.append([(s, 1)])
        if people >= 3:
            side_opts.append([(s, 2)])
        for t in sides[i + 1:]:
            side_opts.append([(s, 1), (t, 1)])
    drink_opts: list[list[tuple[Meal, int]]] = [[]]
    for i, d in enumerate(drinks):
        drink_opts.append([(d, people)])
        for d2 in drinks[i + 1:]:
            for k in range(1, people):
                drink_opts.append([(d, k), (d2, people - k)])
    for so in side_opts:
        for do in drink_opts:
            items = so + do
            extras.append((sum(x.price * q for x, q in items), items))
    extras.sort(key=lambda x: x[0])
    prices = [e[0] for e in extras]

    for ms in _main_sets(mains, people, budget):
        m_total = sum(x.price * q for x, q in ms)
        if m_total > T * CAP:
            continue
        hi = _bisect_right(prices, T * CAP - m_total)
        lo = _bisect_left(prices, T * 0.80 - m_total)
        if hi <= lo:
            continue
        picked = 0
        for idx in range(hi - 1, lo - 1, -1):
            price, items = extras[idx]
            yield _mk([Item(x, q) for x, q in ms] + [Item(x, q) for x, q in items], "alacarte")
            picked += 1
            if picked >= 6:
                break


def _main_sets(mains: list[Meal], people: int, budget: float):
    for i, a in enumerate(mains):
        yield [(a, people)]
        for b in mains[i + 1:]:
            if a.price + b.price > 2 * budget:
                break
            for k in range(1, people):
                yield [(a, k), (b, people - k)]


def _bisect_left(a: list[float], x: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(a: list[float], x: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _mk(items: list[Item], template: str) -> Plan:
    merged: dict[str, Item] = {}
    for i in items:
        if i.meal.code in merged:
            merged[i.meal.code].qty += i.qty
        else:
            merged[i.meal.code] = Item(i.meal, i.qty)
    its = list(merged.values())
    return Plan(items=its, template=template,
                signature=tuple(sorted((i.meal.code, i.qty) for i in its)))


def _score(p: Plan, people: int, budget: float, want: str, priced: bool,
           coupon_codes: set[str] | None = None) -> float:
    """分数 = 预算拟合（主项）+ 组合完整度（小权重）+ 偏好。

    "把预算用好"是这个工具的第一诉求：85% 用掉和 99% 用掉不是一个量级的方案，
    完整度那点加成只是同等拟合度时的排序依据，不能盖过预算项。
    """
    T = people * budget
    total = p.total if priced else p.local_total
    use = total / T
    s = 1.0
    s -= 2.6 * max(0.0, 1.0 - use)      # 用不满：按缺口扣
    s -= 5.0 * max(0.0, use - 1.0)      # 超预算：重罚（正常不会出现，兜底）
    has_combo = any(i.meal.role == "combo" for i in p.items)
    if has_combo:
        s += 0.06                        # 套餐自带主食+小食+饮料
    else:
        if p.qty_by_role("drink") >= people:
            s += 0.04
        if p.qty_by_role("side") >= 1:
            s += 0.03
        if people >= 3 and p.qty_by_role("side") >= 2:
            s += 0.02
    s += 0.01 * min(len({i.meal.code for i in p.items if i.meal.role in ("main", "combo")}), 3)
    if coupon_codes and p.codes() & coupon_codes:
        s += 0.10                        # 这桌能用上券：真金白银，值得优先
    if priced and p.coupon_saving > 0:
        s += min(0.10, p.coupon_saving / max(total, 1) * 0.8)
    if want == "new" and any("上新" in m.tags for m in (i.meal for i in p.items)):
        s += 0.08
    if want == "lowcal":
        kp = _kcal_pp(p, people)
        s += 0.08 if 0 < kp <= 750 else 0.0
    return round(s, 4)


def _diverse(plans: list[Plan], n: int) -> list[Plan]:
    """分数序已排好：套餐制/单点制各先取一个，再按分数补足；
    同一套主餐只出一个方案（避免 6 个结果只差一杯饮料）。"""
    out: list[Plan] = []
    seen_sig: set[tuple] = set()
    main_uses: dict[frozenset, int] = {}

    def main_key(p: Plan) -> frozenset:
        return frozenset(i.meal.code for i in p.items if i.meal.role in ("combo", "main"))

    def take(p: Plan) -> bool:
        k = main_key(p)
        if p.signature in seen_sig or main_uses.get(k, 0) >= 1:
            return False
        if not all(_diff(p, q) for q in out):
            return False
        out.append(p)
        seen_sig.add(p.signature)
        main_uses[k] = main_uses.get(k, 0) + 1
        return True

    for tpl in ("combo", "alacarte"):
        for p in plans:
            if p.template == tpl and take(p):
                break
    for p in plans:
        if len(out) >= n:
            break
        take(p)
    return out[:n]


def _diff(a: Plan, b: Plan) -> bool:
    return a.core() != b.core()


# --- 解释 -----------------------------------------------------------------
def _explain(svc: DineService, p: Plan, store: dict, people: int, budget: float,
             exclude_count: int, order_type: int, be_type: int,
             coupons: list[dict] | None = None) -> None:
    total = p.total
    T = people * budget
    reasons: list[dict] = []

    for it in p.items:
        if it.meal.role not in ("combo", "main"):
            continue
        txt = f"{it.meal.name} ×{it.qty}，{it.meal.price:.1f} 元/份"
        if it.qty > 1:
            txt += f"，占总额 {it.subtotal / max(total, 0.01) * 100:.0f}%"
        reasons.append({"kind": "pick", "text": txt, "badge": "菜单现价"})
        if it.meal.role == "combo":
            d = svc.meal_detail(store, it.meal.code, order_type, be_type)
            delta = _split_saving(d, svc._menu_index, it.meal.price) if d else None
            if delta and delta > 0.5:
                reasons.append({
                    "kind": "value",
                    "text": f"这套拆开单点合计 {it.meal.price + delta:.1f} 元，成套买省 {delta:.1f} 元/份",
                    "badge": "套餐组成核对"})

    diff = T - total
    reasons.append({
        "kind": "budget",
        "text": (f"{people} 人 × {budget:.0f} 元 = {T:.0f} 元预算；实付 {total:.2f} 元，"
                 f"用掉 {total / T * 100:.1f}%，{'余' if diff >= 0 else '超'} {abs(diff):.2f} 元"),
        "badge": "核价"})

    if p.used_coupons:
        for c in p.used_coupons:
            after = round((p.priced_total or 0) - c["saving"], 2)
            reasons.append({
                "kind": "coupon",
                "text": (f"{c['title']}（券 ID …{str(c['couponId'])[-6:]}）挂到「{c.get('productName')}」："
                         f"{p.priced_total:.2f} → {after:.2f}，省 {c['saving']:.2f} 元"),
                "badge": "券后复核"})
    else:
        covers = "、".join(dict.fromkeys(
            (c.get("title") or "") for c in (coupons or []))) or "无"
        reasons.append({"kind": "coupon",
                        "text": (f"这桌没有券可用；该店实时券池 {len(coupons or [])} 张，"
                                 f"覆盖：{covers}"),
                        "badge": "券池"})

    if exclude_count:
        reasons.append({"kind": "avoid",
                        "text": f"已按忌口排除 {exclude_count} 个候选（名称命中 + 套餐组成核实），本方案 0 冲突",
                        "badge": "过滤记录"})

    kp = _kcal_pp(p, people)
    if kp:
        reasons.append({"kind": "kcal",
                        "text": f"人均约 {kp:.0f} kcal（营养成分表命中 {_kcal_coverage(p)} 项）",
                        "badge": "营养表"})
    p.reasons = reasons


def _split_saving(detail: dict, menu_index: dict[str, float], combo_price: float) -> float | None:
    """套餐默认搭配拆成单品单点要多少钱，再减套餐价。组成项缺价则不给结论。"""
    codes = []
    for r in detail.get("rounds") or []:
        for c in r.get("choices") or []:
            if c.get("isDefault") == 1 and c.get("code"):
                codes.append(str(c["code"]))
    if len(codes) < 2:
        return None
    prices = [menu_index.get(c) for c in codes]
    if any(x is None for x in prices):
        return None
    delta = round(sum(prices) - combo_price, 2)
    return delta if delta > 0 else None


def _detail_text(detail: dict) -> str:
    parts = []
    for r in detail.get("rounds") or []:
        for c in r.get("choices") or []:
            if c.get("isDefault") == 1 and c.get("name"):
                parts.append(c["name"])
    return " ".join(parts)


def _has_meat_hint(name: str) -> bool:
    return any(k in name for k in ("牛", "鸡", "猪", "鱼", "鳕", "虾", "培根", "火腿", "肉"))


# --- 营养 -----------------------------------------------------------------
def _parse_nutrition(d) -> list[tuple[str, float]]:
    """MCP 返回 `[160]{productName,...}:` + CSV 行（也可能直接是 JSON 数组）。"""
    rows: list[tuple[str, float]] = []
    if isinstance(d, list):
        for r in d:
            if isinstance(r, dict):
                n = (r.get("name") or r.get("productName") or "").strip()
                k = r.get("kcal") or r.get("energyKcal")
                if n and isinstance(k, (int, float)):
                    rows.append((n, float(k)))
        return rows
    if not isinstance(d, str):
        return rows
    for line in d.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            kcal = float(parts[3])
        except ValueError:
            continue
        if parts[0]:
            rows.append((parts[0], kcal))
    return rows


def _match_kcal(name: str, table: list[tuple[str, float]]) -> tuple[float | None, str]:
    """双向包含匹配。名字被截短时（菜单叫“薯条”、营养表叫“中薯条”）按长度最接近的判定。"""
    exact = None
    rev: list[tuple[str, float]] = []
    for n, k in table:
        if not n:
            continue
        if n in name:
            if exact is None or len(n) > len(exact[0]):
                exact = (n, k)
        elif len(name) >= 2 and name in n:
            rev.append((n, k))
    if exact:
        return exact[1], exact[0]
    if rev:
        rev.sort(key=lambda x: len(x[0]))
        return rev[0][1], rev[0][0]
    return None, ""


def _kcal_pp(p: Plan, people: int) -> float:
    total, known, allq = 0.0, 0, 0
    for i in p.items:
        allq += i.qty
        if i.meal.kcal:
            total += i.meal.kcal * i.qty
            known += i.qty
    # 覆盖不足 3/4 就别报数字：宁可不说，也不给一个"人均 125 kcal"的假精确
    if allq == 0 or known / allq < 0.75:
        return 0.0
    return total / people


def _kcal_coverage(p: Plan) -> str:
    known = sum(i.qty for i in p.items if i.meal.kcal)
    return f"{known}/{sum(i.qty for i in p.items)}"


# --- 输出 -----------------------------------------------------------------
def _plan_json(p: Plan, people: int, budget: float) -> dict:
    T = people * budget
    kp = _kcal_pp(p, people)
    return {
        "title": _title(p, people),
        "template": p.template,
        "items": [{
            "code": i.meal.code, "name": i.meal.name, "qty": i.qty,
            "unitPrice": i.meal.price, "subtotal": i.subtotal, "role": i.meal.role,
            "image": i.meal.image, "kcal": i.meal.kcal, "note": i.note,
            "coupon": bool(i.coupon),
            "couponId": (i.coupon or {}).get("couponId"),
            "couponCode": (i.coupon or {}).get("couponCode"),
            # 券是按「行」生效的：一行 2 份 22.5 元的汉堡，券后整行 11.9 元
            "couponSubtotal": (round(i.subtotal - p.used_coupons[0]["saving"], 2)
                               if i.coupon and p.used_coupons else None),
        } for i in p.items],
        "total": round(p.total, 2),
        "perPerson": round(p.total / people, 2),
        "budgetTotal": round(T, 2),
        "useRatio": round(p.total / T, 4),
        "couponBefore": p.priced_total,
        "couponSaving": round(p.coupon_saving, 2),
        "promoSaving": round(p.promo_discount, 2),
        "kcalPerPerson": round(kp) if kp else None,
        "score": p.score,
        "reasons": p.reasons,
        "topup": p.topup,
        "takeWay": p.take_way,
        "traceIds": p.trace_ids,
        "hasCoupon": p.coupon_saving > 0,
    }


def _title(p: Plan, people: int) -> str:
    mains = [i for i in p.items if i.meal.role in ("combo", "main")]
    names = "、".join(dict.fromkeys(i.meal.name for i in mains))
    if len(names) > 26:
        names = names[:25] + "…"
    prefix = "套餐制" if p.template == "combo" else "单点拼桌"
    return f"{people} 人 · {prefix} · {names}"


def _coupon_json(c: dict) -> dict:
    return {
        "title": c.get("title"),
        "couponId": c.get("couponId"),
        "couponCode": c.get("couponCode"),
        "valid": c.get("tradeDateTime"),
        "products": [p.get("productName") for p in (c.get("products") or [])],
        "productCodes": [p.get("productCode") for p in (c.get("products") or [])],
    }


def _claimable_json(c: dict) -> dict:
    return {"title": c.get("couponName"), "label": c.get("label"),
            "image": c.get("couponImage"), "status": c.get("couponStatus")}


# --- 缓存 -----------------------------------------------------------------
def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)
