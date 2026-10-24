# 麦门配餐 · mcd-dine

> 输入 **人数 / 人均预算 / 忌口 / 到店或外送**，配出一桌麦当劳，并给出**总价、每一条推荐的出处、券省了多少**，最后可以一键建单（付款留给你）。
>
> 不套游戏外壳。目标是「今天中午真能用」。

![overview](docs/demo/shot_top.png)

**演示**：[25 秒真实操作录屏](docs/demo/mcd-dine-demo.mp4) · [使用说明](docs/usage.md) · [封面](docs/demo/cover.png)

---

## 一句话区别

市面上的配餐工具是「按经验配」——它告诉你"3 个人点 3 个套餐差不多"。
**mcd-dine 的每一分钱都来自麦当劳 MCP 的实时数据**：

| | 常见做法 | mcd-dine |
|---|---|---|
| 价格 | 写死 / 估算 | `calculate-price` 逐方案核价，返回分单位原始字段 |
| 券 | 提一句"记得用券" | `query-store-coupons` 拉门店实时券池，挂到具体商品上再核一次价 |
| 理由 | "这样搭配比较均衡" | "麦辣鸡腿汉堡 ×2 挂 11.9 元券：120.00 → 109.40，省 10.60 元" |
| 预算 | 大概齐 | **硬约束**：宁可少几块，绝不超一分 |
| 忌口 | 让你自己看 | 名称+标签过滤，套餐再调 `query-meal-detail` 核实组成 |

---

## 30 秒跑起来

### A. 只想看界面（零配置）

双击 **`index.html`**。

它内嵌了一份 **2026-10-22 22:25 抓的真实菜单快照**（103 项、门店券池 2 张），
所以不联网、不起服务也能改人数/预算/忌口，实时重排。此时数字标「估算」——因为没走核价。

### B. 连接真实 MCP（核价、券、建单都可用）

```bash
# 1) 放 token（只在本机，已被 .gitignore 忽略）
echo 'MCD_MCP_TOKEN=你的token' > .env.local

# 2) 起服务（会自己打开浏览器）
python run.py
# → http://127.0.0.1:8777
```

需要 Python 3.10+，**零第三方依赖**（纯标准库；MCP 走 Streamable HTTP 手写实现）。

命令行也能用：

```bash
python run.py demo --people 3 --budget 40 --avoid beef,spicy --keyword 湖滨银泰
```

---

## 它怎么工作

```
人数/预算/忌口/位置
      │
      ├─ query-nearby-stores ──→ 门店（营业状态、营业时间）
      ├─ query-meals ─────────→ 103 项在售 + 分类 + 标签 + 现价
      ├─ query-store-coupons ─→ 门店实时券池（券ID/券码/适用商品）
      ├─ list-nutrition-foods → 营养成分表（160 项）
      │
      ▼  本地枚举：套餐制 / 单点拼桌 两族，约 2000 个候选，零网络
      │
      ▼  打分：预算拟合为主，完整度/多样性/口味偏好为辅；同一套主餐只出一个方案
      │
      ▼  只对 Top-6 调 calculate-price（真金白银的核价）
      │
      ▼  含券可用商品的方案再核一次「券后价」；券省下来的钱够一份小食就加购再核
      │
      ▼  输出：6 个方案 + 每条理由 + 调用链 traceId
```

**为什么这么设计**：MCP 接口限流 600 次/分钟，而且每次往返 300~500ms。
把 35 个工具丢给模型挨个试错，既慢又贵还可能限流。
这里改成「**本地枚举 + 少量核价**」：一次配餐 12~16 次调用、5~7 秒出结果。

---

## 三个真问题（都是实测踩出来的）

**1. 门店 21:00 打烊，接口直接报错**
`query-meals` 返回 `600057 门店可能已关闭或不在营业时间`。产品不能白屏——现在会明确告诉你
「这家 06:00-21:00，此刻不在营业时间」，并列出附近还在营业的门店（或提示换 24 小时店）。

**2. 券对「整行」生效（重要）**
一行 3 份 22.5 元的麦辣鸡腿堡挂 11.9 元券，`calculate-price` 返回 **11.90 元**——它把整行当成 1 份算。
如果照抄，页面会显示"3 个汉堡 11.9 元"，看着像作弊，真下单也不是这个价。
**处理**：券行拆成「1 份带券 + 其余原价」，实测返回 56.90 元（= 11.9 + 45），才是真实金额。

**3. 套餐热量不能拿主堡冒充**
`list-nutrition-foods` 只有单品。给「麦辣鸡腿汉堡三件套」匹配到 485 kcal 会严重低估（还含薯条和可乐）。
**处理**：套餐不报热量，只对单品报；覆盖率不足 3/4 就整桌不报——**宁可不说，也不给假精确**。

---

## 数据来源与边界

- 数据：麦当劳中国 MCP（`mcp.mcd.cn`），用到 `query-nearby-stores` / `query-meals` /
  `query-meal-detail` / `query-store-coupons` / `available-coupons` / `auto-bind-coupons` /
  `calculate-price` / `list-nutrition-foods` / `create-order`。
- **不做**：不自动付款、不代点、不猜价格。建单会再核一次价，金额和页面显示不一致就拒绝下单。
- token 只存在本机 `.env.local`（已 gitignore），浏览器拿不到。
- 券价/满减会随活动变化，`docs/evidence/` 里的记录都带时间戳与 traceId。

## 真实调用证据

`docs/evidence/2026-10-22-live-calls.md` —— 找店 / 套餐组成 / 券前券后核价各一次，
每次调用都带 traceId，可对照麦当劳侧日志。摘录：

| | 实付 | 原始字段 |
|---|---|---|
| 麦辣鸡腿汉堡 ×3 券前 | 67.50 元 | `price=6750` |
| 同一单挂 11.9 元券 | 56.90 元 | `price=5690 discount=1060` |

## 来源声明

本项目**复用**了同作者上一个作品 [mcd-rogue](https://github.com/langlangagowith/mcd-rogue)
的 MCP 调用层（`scripts/mcp_client.py`：Streamable HTTP + SSE 解析）。

本次新增：`MCD_MCP_URL` 覆盖、`call()` 统一错误与 trace、429 退避、
配餐内核 `dine_service.py`、本地服务 `serve.py`、单文件前端、离线快照与构建脚本、
以及「券行拆分」「套餐热量不冒充」两条数据纪律。

## 目录

```
run.py                 入口：默认起服务；demo 子命令跑命令行
index.html             构建产物（双击即用，内嵌离线快照）
src/mcp_client.py      MCP 客户端（复用 + 增强）
src/dine_service.py    配餐内核：枚举 / 评分 / 核价 / 券 / 解释
src/serve.py           本地 HTTP 服务（127.0.0.1:8777）
web/app.html           前端源码（构建时注入快照 → index.html）
scripts/build_web.py   app.html + data/offline.json → index.html
scripts/fetch_snapshot.py  抓离线快照
scripts/rehearse.py    录真实调用证据
docs/evidence/         真实调用记录（md + json）
docs/usage.md          使用说明（提交比赛用）
docs/demo/             演示录屏 / 截图 / 封面
docs/pitch.md          3 分钟路演稿
workbuddy.md           用 WorkBuddy 做这个东西的过程记录
```

## License

MIT

---

## 交付状态（2026-10-25）

- ✅ 内核 / 前后端 / 离线快照 / 真实调用证据 / README / workbuddy.md / 路演稿
- ✅ **已开源**：<https://github.com/langlangagowith/mcd-dine>（`main` = `b5aa6ca`，根目录含 `workbuddy.md`）
- ✅ 提交物料：`docs/demo/mcd-dine-demo.mp4`（25 秒真机录屏）· `docs/demo/cover.png`（封面）· `docs/usage.md`（使用说明）
- ⬜ 比赛提交（1024 超能 Coding · 用餐赛道，10-25 16:00 截止）：赛道选「用餐」＋使用工具勾 **WorkBuddy**
