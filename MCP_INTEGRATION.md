# MCP 集成说明（MCP_INTEGRATION.md）

本文说明「麦门省钱搭子」如何真实接入并使用**麦当劳官方 MCP Server**，
包括 Server 信息、用到的 Tool、端到端调用流程、实测数据与业务价值。

> 本文所有工具名、参数与返回字段均来自对 MCP Server 的**实际探测**（`tools/list` 返回 35 个工具），
> 不含任何推测或编造。

---

## 1. MCP Server

| 项 | 值 |
|---|---|
| 名称 | `mcd-mcp` |
| 版本 | `1.0.0` |
| Endpoint | `https://mcp.mcd.cn` |
| 传输 | Streamable HTTP（JSON-RPC 2.0 over POST） |
| 协议版本 | `2024-11-05` |
| 鉴权 | `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 工具总数 | **35** |
| Token 获取 | <https://open.mcd.cn/mcp> → 登录 → 控制台 → 激活 → 申请 Token |

连通性实测（`python src/mcp_client.py --check`）：

```text
serverInfo : {'name': 'mcd-mcp', 'version': '1.0.0'}
protocol   : 2024-11-05
tools      : 35
```

---

## 2. 本作品实际调用的 Tool

调用链按「定位门店 → 取菜单 → 取优惠 → 真实算价」组织，共 **5 个核心工具**：

| # | Tool | 作用 | 关键参数 |
|---|---|---|---|
| 1 | `delivery-query-addresses` | 外送场景取用户地址（拿 `addressId`） | 无 |
| 1' | `query-nearby-stores` | 到店场景搜门店（拿 `storeCode` / `beCode`） | `beType`, `searchType`, `city`, `keyword` |
| 2 | `delivery-query-stores` | 按地址查可配送门店 | `addressId`, `beType` |
| 3 | `query-meals` | 取门店菜单（商品名、价格、可售状态） | `storeCode`, `orderType`, `beType`（到店自取不传 `beCode`） |
| 4 | `query-store-coupons` | 取该门店可用的券 | `storeCode`, `orderType`, `beType` |
| 5 | **`calculate-price`** | **按真实优惠规则算价（含配送费、打包费、优惠）** | `storeCode`, `orderType`, `beType`, `items[]` |

辅助工具（按需）：

| Tool | 用途 |
|---|---|
| `query-meal-detail` | 套餐组成、可特调项 |
| `available-coupons` / `auto-bind-coupons` | 领取/查看可领券 |
| `query-my-coupons` | 卡包内已有券 |
| `list-nutrition-foods` | 营养成分（热量约束场景） |
| `now-time-info` | 服务器时间（判断活动/券有效期） |
| `campaign-calendar` | 当月营销活动日历 |

### 参数传递规则（易错点，已在代码中固化为常量）

麦当劳 MCP 对 `beType` / `orderType` / `beCode` 的组合有严格约束，传错即报错：

| 场景 | `beType` | `orderType` | `beCode` |
|---|---|---|---|
| 到店自取 | 1 | 1 | **不传**（传了会报错） |
| 得来速 DT | 5 | 1 | 必传（来自 `query-nearby-stores`） |
| 麦乐送 | 2 | 2 | 必传（来自 `delivery-query-stores`） |
| 企业团餐 | 6 | 2 | 必传 |

代码实现见 `src/mcp_client.py` 的 `BE_TYPE` / `ORDER_TYPE` 常量与 `meals()` / `calculate_price()`。

---

## 3. 端到端调用流程

```text
用户输入（人数、预算、忌口）
        │
        ▼
[1] 定位门店
    外送：delivery-query-addresses → delivery-query-stores
    到店：query-nearby-stores(beType=1, searchType=2, city, keyword)
        │  得到 storeCode / beCode / storeName
        ▼
[2] query-meals → 得到该门店全部可售商品与价格（实测本次 109 个）
        │
        ▼
[3] 单价校准（关键）
    菜单的 currentPrice 对套餐类商品可能只是「最低价起售」，
    与真实成交价差异很大（实测四件套 ¥28 vs 实际 ¥52）。
    因此对最便宜的一批候选逐个调用 calculate-price(quantity=1) 探真实单价。
        │
        ▼
[4] 组合搜索（本地）
    硬约束过滤（预算/忌口/忌辣）→ 品类分组 → 候选枚举 → 加权评分 → 取 top N
        │
        ▼
[5] calculate-price 逐个候选真实算价（最多 12 次）
        │  得到 price（实付）/ originalPrice / discount / deliveryPrice / packingPrice
        ▼
[6] 以真实价筛选：只推荐「实付 ≤ 预算」的方案；无解时如实说明差额
        │
        ▼
[7] 输出 2～3 个可比较方案（含总价、人均、费用拆分、节省、理由）
```

### 实测调用样例（真实返回，已脱敏）

```jsonc
// calculate-price 返回（价格单位为「分」）
{
  "success": true,
  "data": {
    "price": 5900,            // 实付 ¥59.00
    "originalPrice": 5980,    // 原价 ¥59.80
    "discount": 80,           // 优惠 ¥0.80
    "productPrice": 5000,     // 商品小计 ¥50.00
    "deliveryPrice": 600,     // 配送费 ¥6.00
    "packingPrice": 300,      // 打包费 ¥3.00
    "productList": [
      { "productCode": "9900016057", "productName": "汉堡开心乐园餐",
        "quantity": 2, "originalSubtotal": 5000, "subtotal": 5000 }
    ]
  }
}
```

> 注意：价格字段统一以「分」为单位，代码中已统一除以 100 换算为元。

---

## 4. 真实调用结果（可复核）

在门店 `3330419`（麦当劳绍兴上虞上百万悦城餐厅）实测：

| 指标 | 实测值 |
|---|---|
| 门店可售商品 | 109 个 |
| 菜单分类 | 14 个 |
| 单价校准修正的商品 | 16 个 |
| 某方案真实算价 | 商品 ¥90.70，优惠 -¥29.30，实付 ¥90.70 |
| 外送场景附加费 | 配送费 ¥6.00 + 打包费 ¥3.00（按件数浮动） |

复现命令：

```bash
# 到店自取
python src/meal_planner.py --people 2 --budget 130 --mode pickup --city 绍兴 --keyword 上虞

# 外送（自动取账号已有地址）
python src/meal_planner.py --people 2 --budget 150 --mode delivery

# 只要 JSON（便于接入其他前端）
python src/meal_planner.py --people 2 --budget 130 --mode pickup --city 绍兴 --keyword 上虞 --json
```

---

## 5. 业务价值

1. **把「查菜单」升级为「可执行的决策」**
   官方 APP 能看菜单，但不会回答「2 人 130 元怎么点最划算还管饱」。
   本作品输出的是带费用拆分和取舍理由的**方案集**，而不是商品列表。

2. **用真实优惠算价，不猜价格**
   多数同类作品用静态价目表或大模型臆测价格；本作品每一分钱都来自
   `calculate-price`，包含配送费、打包费与优惠，避免"看起来 50 实际 70"。

3. **硬约束优先，拒绝糊弄**
   预算、忌口、忌辣不可违反；预算内无解时**如实说明还差多少钱**，
   而不是硬凑一个超预算方案。

4. **发现并处理了官方数据的语义陷阱**
   - `currentPrice` 对套餐只是最低起售价 → 增加真实算价校准步骤；
   - `canWithOrder` 只对 16 个套餐为 true，但单品实际可算价可下单
     → 不把它当硬过滤，否则方案多样性归零；
   - 价格单位为「分」、外送含配送与打包费 → 统一换算与展示。

   这些细节只有**真正调用过接口**才会遇到，也是本作品区别于静态 Demo 的地方。

---

## 6. 安全与合规边界

- MCP Token 从环境变量 `MCD_MCP_TOKEN` 读取，**不写入仓库、不打印到日志**；
  本地开发回退读取 WorkBuddy 连接器配置，同样不落盘到仓库。
- 只做「推荐」，**不自动下单、不自动支付**；任何创建订单动作须用户明确确认。
- 不输出、不记录用户手机号、完整地址、订单号等敏感信息（示例已脱敏）。
- 所有商品、价格、券信息均来自官方 MCP 实时返回，不缓存过期数据当作实时结果。
