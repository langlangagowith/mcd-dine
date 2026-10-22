# 真实调用记录（自动生成）
> 生成时间：2026-10-22 22:53:31　·　生成脚本：`scripts/rehearse.py`
> 服务端：麦当劳中国 MCP `mcp.mcd.cn`　·　总耗时 2.1s　·　共 6 次调用

这份文件里的每个价格、券号、traceId 都是一次真实网络往返的结果，没有手工改过。

## 0. 场景
- 门店：**麦当劳杭州上城区延安路东坡宾馆餐厅**（`storeCode=3330604`，营业 07:00-23:00）
- 城市/位置：杭州市 · 湖滨银泰　·　人数：3
- 菜单在售：103 项　·　门店券池：2 张

## 1. 套餐组成（query-meal-detail）
**麦辣鸡腿汉堡三件套**（`code=9900005456`，菜单价 33.50 元） = 麦辣鸡腿汉堡 + 中薯条 + 可乐中杯

## 2. 券前 / 券后（同一份商品，只差一张券）
商品：**麦辣鸡腿汉堡** ×3（`productCode=1440`）
套餐组成（`query-meal-detail` 默认选项）：麦辣鸡腿汉堡 + 中薯条 + 可乐中杯

| | 实付 | calculate-price 原始字段 |
|---|---|---|
| 券前 | **67.50 元** | `price=6750` `discount=0` |
| 券后 | **56.90 元** | `price=5690` `discount=1060` |

省 **10.60 元**。券：**11.9元麦辣鸡腿堡**　`couponId=A255280326E3328D179C362B7B8DDA65`　`couponCode=MCD6150702B27V3300390`

## 3. 逐次调用（工具 → traceId）
| # | 工具 | 返回码 | 耗时 | traceId |
|---|---|---|---|---|
| 1 | `query-nearby-stores` | 200 | 422ms | `df5b576770b7756e1bb6dbcbb9a48df4` |
| 2 | `query-meals` | 200 | 485ms | `a005e46bbf55b83a66c2264e0f7118e1` |
| 3 | `query-store-coupons` | 200 | 261ms | `298fbc146f710b1b4455402b87be892f` |
| 4 | `query-meal-detail` | 200 | 334ms | `6637004a419274ddfdd02d5457032309` |
| 5 | `calculate-price` | 200 | 294ms | `c36eee6cf9fe7f1b5f60350e75b9a5d4` |
| 6 | `calculate-price` | 200 | 317ms | `a89d020107c98acc0787b989816d0b45` |

## 4. 怎么复核
```bash
python src/mcp_client.py call query-nearby-stores '{"beType":1,"searchType":2,"city":"杭州市","keyword":"湖滨银泰"}'
python src/mcp_client.py call query-meal-detail '{"storeCode":"3330604","orderType":1,"beType":1,"code":"1440"}'
python src/mcp_client.py call calculate-price '{"storeCode":"3330604","orderType":1,"beType":1,"items":[{"productCode":"1440","quantity":1,"couponId":"A255280326E3328D179C362B7B8DDA65","couponCode":"MCD6150702B27V3300390"},{"productCode":"1440","quantity":2}]}'
```

> 券价会随活动结束/库存变化，复跑时数字可能不同——**同一天同门店同券**应当一致。
