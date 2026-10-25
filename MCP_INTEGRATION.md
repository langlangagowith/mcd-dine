# MCP 集成说明 · MCP_INTEGRATION

本文说明「麦门配餐 mcd-dine」**实际**使用了哪些麦当劳 MCP 能力、调用流程如何编排，以及这些能力带来的业务价值。所有数字均来自本仓库代码与真实调用记录（`_probe/` 下的 24h 原始返回），不是设计稿。

## 1. 接入信息

| 项 | 值 |
|---|---|
| MCP Server | `https://mcp.mcd.cn` |
| 传输方式 | `streamablehttp`（MCP 协议 `initialize` → `tools/list` → `tools/call`） |
| 鉴权 | `Authorization: Bearer <MCP_TOKEN>`，token 由使用者在 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 自助申请 |
| 客户端实现 | `src/mcp_client.py`（自研轻客户端，约 200 行，零第三方依赖） |
| 服务端工具数 | `tools/list` 返回 **35** 个工具 |
| 本项目实际使用 | **11** 个（见下表） |

Token 不进仓库、不进日志：`src/mcp_client.py` 先读环境变量 `MCD_MCP_TOKEN`，没有则读仓库根目录的 `.env.local` / `.env`（两者均在 `.gitignore` 中）；都找不到时直接报错，不会静默降级成假数据。

## 2. 实际调用的 Tool

| # | Tool | 用途 | 在流程中的位置 | 关键返回字段 |
|---|---|---|---|---|
| 1 | `query-nearby-stores` | 按城市 / 关键词查门店 | 第 1 步（到店自取） | `storeCode` `storeName` `storeAddress` |
| 2 | `delivery-query-stores` | 按坐标 / 地址查可外送门店 | 第 1 步（外送） | `storeCode` `deliveryFee` `minPrice` |
| 3 | `delivery-query-addresses` | 取账号已存收货地址 | 第 1 步（外送） | `addresses[].addressId` |
| 4 | `query-meals` | 拉门店全量菜单（含套餐 / 单点 / 小食 / 饮料 / 甜品） | 第 2 步 | `productCode` `name` `price` `categoryType` |
| 5 | `query-meal-detail` | 取单品明细（套餐内含、可选规格、加价项） | 第 2 步（按需） | `products[]` `optionalProducts[]` |
| 6 | `list-nutrition-foods` | 取营养表（热量 / 蛋白质等） | 第 2 步（按需） | `calories` `protein` 等 |
| 7 | `query-store-coupons` | 拉该门店券池（可领 / 已领） | 第 3 步 | `couponId` `couponCode` `couponThreshold` `discount` `productCodes` |
| 8 | `available-coupons` | 查账号当前可用券 | 第 3 步（校验） | `couponList[]` |
| 9 | `auto-bind-coupons` | 一键领取门店券（**用户点按钮才触发**，不自动替用户领） | 第 3 步（用户动作） | `couponCount` |
| 10 | `calculate-price` | 逐方案核价（含券后价、优惠明细） | 第 4 步（核心） | `price` `originalPrice` `couponSubtotal` `traceId` |
| 11 | `create-order` | 建单（**付款留给人**，本项目只到建单为止） | 第 5 步（用户确认） | `orderId` `payUrl` |

> 未使用的能力（如积分、活动、卡券核销、订单查询等）在代码里没有分支，本文不作夸大。

## 3. 一次配餐的调用时序

输入：人数、人均预算、忌口（可多选）、门店或收货地址。

1. **门店**：到店自取走 `query-nearby-stores`；外送走 `delivery-query-addresses` → `delivery-query-stores`。→ 1 次调用
2. **菜单**：`query-meals` 拉全量菜单（实测 103 项）。→ 1 次调用（进程内缓存 90 秒）
3. **券池**：`query-store-coupons` 拿门店券；必要时 `available-coupons` 复核。→ 1~2 次调用
4. **本地枚举（零网络）**：按品类角色拆分菜单，枚举两族方案——
   - 每人一份套餐（可 1~2 种）；
   - 单点拼桌（N 份主食 + 共享小食 + N 杯饮料）。

   约 **2000 个候选**在本地完成总价与预算拟合打分，**不产生任何 MCP 调用**。
5. **Top-6 核价**：只把得分最高的 6 个方案送 `calculate-price`，拿到真实成交价与优惠明细。→ 6 次调用
6. **券后价**：方案里含可挂券商品时，把券（`couponId` + `couponCode`）挂到 `items` 上再核一次价。→ 2~4 次调用
7. **凑单**：若券省下的钱够一份小食，加购后**再核一次价**，确认没超预算。→ 1~2 次调用

**一次配餐合计 12~16 次调用、5~7 秒**（UI 上直接显示「MCP 调用 N 次」）。

## 4. 证据链：每个数字都能回溯

- 每次 `tools/call` 都记录 `工具名 / traceId / 耗时 / 返回码`，存于 `client.trace` 与结果的 `traceIds[]`。
- 页面每个推荐方案可展开「依据」：现价、券 ID、核价前后差额（例：`麦辣鸡腿堡 ×2 挂 11.9 元券：120.00 → 109.40，省 10.60 元`），逐条对应一次真实调用。
- 原始返回留档在 `_probe/*.json`（`02_meals.json`、`03_store_coupons.json`、`21_price_coupon.json` 等），可离线复核。

## 5. 限流与容错

- 接口限流 **600 次/分钟**：`mcp_client.py` 对任意失败（含 429）按 `2^n` 退避重试，默认 2 次重试（共 3 次尝试），仍失败则抛错并在 UI 上写明原因，不做静默兜底。同时把"调用次数"当成成本指标显示出来，避免方案膨胀。
- 菜单 / 券池 / 可领券 / 营养表按 TTL 缓存（90s / 60s / 120s / 600s），连续调整人数预算时不会重复打接口。
- **不做**"让模型拿 35 个工具逐个试错"：那会把一次配餐从 15 次调用放大到几十次，还不可复现。

## 6. 业务价值

| 真实痛点 | MCP 能力如何解决 | 可验证结果 |
|---|---|---|
| 中午 11:50，3 个人、人均 40、有人不吃牛肉——看菜单和券根本算不出"这单划不划算" | `query-meals` + `query-store-coupons` + `calculate-price` 组合，把"菜单价"换成"带券成交价" | 每条推荐给出现价 / 券 / 差额，预算不超一分 |
| 券的规则看不懂（满减门槛、适用商品） | 券只挂在 `productCodes` 命中的 item 上，核价由官方接口裁决 | 券后价由 `calculate-price` 返回，不靠本地猜 |
| 忌口容易被当成关键词过滤 | 忌口先落到商品属性与套餐明细（`query-meal-detail`），成分不明的套餐直接排除 | 不吃牛肉时，含牛肉的套餐整条不出现在结果里 |
| 决定之后还要手动一格格点单 | `create-order` 生成订单，付款留给人 | 从"想吃什么"到"待付款订单"一步 |

## 7. 已知边界（写清楚，不粉饰）

1. **券对整行生效**：3 份 22.5 元汉堡挂一张 11.9 元券，接口返回 `price=1190`（把整行都按券价算）。照抄会得出"3 个汉堡 11.9 元"的错误结论，本项目按"单份优惠 + 其余原价"折算并标注折算口径。
2. **离线快照不是实时价**：双击 `index.html` 时使用内嵌的 10-22 真实菜单快照（103 项），页面把数字标为「估算」；只有 `python run.py` 连真 MCP 时才核真价。
3. **券池随门店与账号变化**：新账号初始券池可能为空（实测 2 张咖啡券且商品不在菜单），需用户自己点「领券」（`auto-bind-coupons`）后才有券后价。
4. **不代替下单决策**：建单后仍需用户在麦当劳官方渠道付款，本项目不保存支付信息。

## 8. 复现方式

```bash
git clone https://github.com/langlangagowith/mcd-dine && cd mcd-dine
echo "MCD_MCP_TOKEN=<你的 MCP Token>" > .env.local   # 或 export MCD_MCP_TOKEN=...
python run.py                                                            # → http://127.0.0.1:8777
python run.py demo --people 3 --budget 40 --avoid beef --keyword 湖滨银泰  # 命令行跑一次，打印调用与核价
```

`demo` 子命令会真实调用 MCP 并把每个方案的 `traceIds` 打出来，可用来核对本文所有数字；`stores` 子命令只查门店。
