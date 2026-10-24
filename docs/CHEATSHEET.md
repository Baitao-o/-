# 麦门省钱搭子 · 全部使用方法速查

> 一份覆盖所有入口的速查表。完整说明见 `docs/USAGE.md`，技术细节见 `MCP_INTEGRATION.md`。
>
> **一个内核、四种入口**：对话（Skill）/ 网页 / 命令行 / HTTP API
> 全部调用同一份 `src/meal_planner.py` 与同一套麦当劳 MCP 算价链路。

---

## 0. 三分钟上手

```bash
# ① 装 Python 3.8+（勾选 Add Python to PATH）
# ② 配 MCD Token（任选一种，推荐第一种）
#      a. 启动网页后，顶部引导条里粘贴保存
#      b. 设置环境变量 MCD_MCP_TOKEN
#      c. 复制 mcp-config.example.json → ~/.maimen-budget-meal/config.json 填 token
# ③ 启动
```

**Windows**：双击 `start.cmd`
**macOS / Linux**：`./start.sh`
**命令行**：`python scripts/skill_boot.py`

浏览器打开控制台打印的地址（默认 `http://127.0.0.1:8808/`，端口被占会自动往后找）。

---

## 1. 对话方式（装成 Skill 后用）

把 `SKILL.md` 复制到 `~/.dsh/skills/mcd-budget-meal/SKILL.md`，之后对 AI 说：

| 你说 | AI 会做 |
|---|---|
| 「打开麦门省钱搭子」 | 调 `skill_boot.py` 拉起服务 → 给你网址 |
| 「两个人 100 块怎么点」 | 直接跑命令行给 2~3 个方案（不开网页） |
| 「要一份主食一份小吃一份甜品一份饮料」 | 走结构模式求最低价 |
| 「想吃鸡翅，不吃辣」 | 想吃偏好 + 忌辣 |
| 「抽个转盘随便来一顿」 | 让你打开网页的转盘 Tab |
| 「就这个下单吧」 | 进真实下单流程（需二次确认） |

触发词族：`打开/启动 + 点餐界面`、`帮我点麦当劳`、`预算X怎么吃`、`几个人怎么配`、`想吃XX`、`抽个转盘`、`麦门`。

---

## 2. 网页方式（三个模式 + 下单）

### 模式 A：按结构配 · 求最低价

1. 顶部选「按结构配 · 求最低价」
2. 填**预算**、**人数**、**取餐方式**（到店自取需填城市+关键词；麦乐送自动用账号地址）
3. 用步进器设置：🍔主食 / 🍟小吃 / 🍦甜品 / 🥤饮品 各几份（0 = 不要）
4. 可选：「每人一份」（份数 ×人数）、「不吃辣」、想吃、忌口
5. 点「给我这个结构的最低价」

**输出**：该结构下**实付最低**的组合 + 每项单价 + 费用拆分 + 预算剩余进度条。

### 模式 B：帮我搭配 · 推荐方案

1. 顶部选「帮我搭配 · 推荐方案」（结构步进器会隐藏）
2. 只填预算（+ 人数、取餐方式）
3. 点「帮我搭配几个方案」

**输出**：2~3 个策略不同的方案（最省钱 / 均衡饱腹 / 丰富尝鲜），各带商品明细、人均、节省。

### 模式 C：🎰 美食转盘 · 随机抽

1. 顶部选「🎰 美食转盘 · 随机抽」
2. 填预算、人数 → 点「🎰 生成转盘」
3. 出现四个独立转盘（主食/小吃/甜品/饮品），每盘放入该品类真实候选
4. 各盘点「转」，或一键「🎰 全部转起来」
5. 满意的打勾**锁定**，只重转其他几个
6. 四格都抽到后点「✅ 就这个，算真实价格」

**规则**：预算按 `÷4` 均分到四类，每盘只放单价不超过额度的商品；抽到什么算什么，不偷偷替换。

### 下单与支付（真实订单）

任一模式算出方案后 → 点**下单**：

```
① 真实订单确认弹窗（商品/门店/取餐方式/应付金额）
② 【默认】🧪 演示模式开启 → 点确认只出演示卡，不建单
   要真正下单：取消勾选「演示模式」
③ 勾选「我确认这是真实订单」（不勾按钮是灰的）
④ 点「确认创建订单」
⑤ 支付引导卡：
   [💳 去支付]      → 打开官方支付页 m.mcd.cn/.../scanToPay
   [取消订单]       → 立即取消
   [我已支付，查询状态] → 查真实订单状态
```

**钱不经本作品**：付款在麦当劳官方页完成，我们不接触任何支付凭证。

---

## 3. 命令行方式（全部参数）

```bash
cd <仓库根目录>
python src/meal_planner.py --budget 100 [其他参数]
```

### 必填

| 参数 | 说明 |
|---|---|
| `--budget` | 总预算（元），必填 |

### 场景与门店

| 参数 | 说明 |
|---|---|
| `--people N` | 用餐人数，默认 2 |
| `--mode pickup\|delivery\|drivethru\|catering` | 到店自取 / 麦乐送 / 得来速 / 企业团餐<br>**注意默认是 `delivery`** |
| `--city 城市` | 到店搜索必填 |
| `--keyword 位置词` | 到店搜索必填 |
| `--store storeCode` | 直接指定门店，跳过搜索 |
| `--be-code beCode` | 得来速/外送场景需要 |

### 结构模式（指定份数）

| 参数 | 说明 |
|---|---|
| `--main N` | 主食份数 |
| `--snack N` | 小吃份数 |
| `--dessert N` | 甜品份数 |
| `--drink N` | 饮品份数 |
| `--per-person` | 份数按人数翻倍（默认是总份数） |

### 偏好

| 参数 | 语义 | 说明 |
|---|---|---|
| `--want 鸡翅,甜品` | **软偏好** | 优先出现，并说明比最低价贵多少 |
| `--avoid 牛肉,香菜` | **硬约束** | 命中即排除，不可违反 |
| `--no-spicy` | 硬约束 | 排除辣味商品 |
| `--no-drink` | 偏好 | 默认模式不需要饮料 |
| `--want-dessert` | 偏好 | 默认模式希望带甜品 |

### 输出

| 参数 | 说明 |
|---|---|
| `--json` | 输出结构化 JSON（便于接入其他前端） |

### 常用配方

```bash
# ① 预算出 2~3 个方案（到店，2 人 130 元）
python src/meal_planner.py --people 2 --budget 130 --mode pickup \
  --city 绍兴 --keyword 上虞

# ② 结构配齐求最低价（1主食1小吃1甜品1饮品，预算 100）
python src/meal_planner.py --budget 100 --main 1 --snack 1 --dessert 1 --drink 1 \
  --mode pickup --city 绍兴 --keyword 上虞

# ③ 每人一份（2 人 → 各类 2 份）
python src/meal_planner.py --budget 200 --main 1 --snack 1 --drink 1 \
  --per-person --mode pickup --city 绍兴 --keyword 上虞

# ④ 想吃鸡翅 + 不吃辣
python src/meal_planner.py --people 4 --budget 100 --no-spicy --want 鸡翅 \
  --mode pickup --city 绍兴 --keyword 上虞

# ⑤ 忌口牛肉
python src/meal_planner.py --budget 100 --main 1 --snack 1 \
  --avoid 牛肉 --mode pickup --city 绍兴 --keyword 上虞

# ⑥ 外送（自动用账号已有地址）
python src/meal_planner.py --people 2 --budget 150 --mode delivery

# ⑦ 输出 JSON
python src/meal_planner.py --budget 100 --main 1 --snack 1 \
  --mode pickup --city 绍兴 --keyword 上虞 --json
```

---

## 4. HTTP API（接入其他前端/自动化）

服务启动后基址 `http://127.0.0.1:8808`。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查（含 `mcpConfigured` / `mcpSource`） |
| GET | `/api/config` | 查 Token 配置状态（只返回掩码） |
| POST | `/api/config` | 保存 Token（`{token}`） |
| POST | `/api/config/clear` | 清除本机 Token |
| POST | `/api/plan` | 出方案（结构份数 >0 走结构模式，全 0 走默认模式） |
| POST | `/api/wheel/pool` | 生成四个盘的候选池 |
| POST | `/api/wheel/settle` | 对抽中商品真实算价 |
| POST | `/api/order/prepare` | 算价 + 返回 `takeWayList`（不建单） |
| POST | `/api/order/create` | 建单（`dryRun` 默认 true） |
| POST | `/api/order/cancel` | 取消订单 |
| POST | `/api/order/query` | 查订单状态 |

### `/api/plan` 请求体

```json
{
  "budget": 100, "people": 2, "mode": "pickup",
  "city": "绍兴", "keyword": "上虞",
  "main": 1, "snack": 1, "dessert": 1, "drink": 1,
  "avoid": "牛肉", "want": "鸡翅",
  "noSpicy": false, "perPerson": false
}
```

- 结构份数 > 0 → `mode:"structure"` + `best`
- 结构全 0 → `mode:"budget"` + `plans`
- 配不齐 → `ok:true, infeasible:true, minimumNeeded, gap`

### `/api/order/create` 请求体

```json
{
  "storeCode": "3330419", "beType": 1, "orderType": 1,
  "takeWayCode": "take-in-store",
  "items": [{"code": "521951", "quantity": 1}],
  "dryRun": false, "confirmed": true
}
```

> `dryRun` 默认 `true`（只预览不建单）；`dryRun:false` **必须**同时 `confirmed:true`，
> 否则返回 400。`takeWayCode` 必须来自 `/api/order/prepare` 的 `takeWayList`。

---

## 5. 配置 Token（三种方式）

优先级：**环境变量 > 本机配置文件 > 本机 WorkBuddy 配置（开发兜底）**

| 方式 | 怎么做 |
|---|---|
| **网页填（推荐）** | 启动后顶部引导条粘贴保存，写入 `~/.maimen-budget-meal/config.json` |
| 环境变量 | `set MCD_MCP_TOKEN=你的Token`（Windows）/ `export MCD_MCP_TOKEN=...` |
| 配置文件 | 复制 `mcp-config.example.json` → `~/.maimen-budget-meal/config.json` |

**申请地址**：<https://open.mcd.cn/mcp> → 手机号登录 → 控制台 → 激活 → 申请 Token

**验证**：`curl -s http://127.0.0.1:8808/api/config`，看 `source` 字段

| source | 含义 |
|---|---|
| `file` / `env` | ✅ 正常 |
| `workbuddy` | 读到本机 WorkBuddy 配置，仅开发机如此 |
| `none` | 未配置，界面会出引导 |

> 每个使用者必须用自己的 Token（积分/券/地址与账号绑定，不可共用）。

---

## 6. 启动方式对照

| 方式 | 命令 | 适用 |
|---|---|---|
| 双击 | `start.cmd` / `start.sh` | 演示、非技术用户 |
| Skill 启动器 | `python scripts/skill_boot.py` | AI 调用；已在跑则不重复启动 |
| 直接起服务 | `python web/server.py` | 想自己看日志 |
| 状态探测 | `python scripts/skill_boot.py --status` | 只看有没有在跑 |
| JSON 输出 | `python scripts/skill_boot.py --json` | 供程序解析 |

环境变量：`PORT`（起始端口，默认 8808）、`HOST`（默认 127.0.0.1）、`OPEN_BROWSER=0`（不开浏览器）。

**端口自适应**：8808 被占用会自动尝试 8809、8810…，一律以控制台打印的地址为准。

---

## 7. 自测脚本

| 脚本 | 作用 |
|---|---|
| `python web/test_api.py` | 接口冒烟：结构模式 ×2 + 预算模式 ×1 |
| `python web/test_consistency.py` | 一致性测试 6 项：忌口/忌辣/perPerson/结构精确/预算无解/周边过滤 |

---

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 页面打得开但查询报错 | Token 未配或过期，重新申请 |
| `source: workbuddy` | 读到本机 WorkBuddy 配置，建议换成自己的 |
| 门店查不到 | `--city` / `--keyword` 组合不对，换词或改用麦乐送 |
| 提示「没有满足条件的组合」 | 预算太紧，会给出最低所需金额 |
| 提示「本店菜单里没有 X」 | 该商品本店不卖，**不是预算问题** |
| 提示「被忌口排除」 | 有该商品但被 `--avoid` / `--no-spicy` 排掉 |
| 周边商品不能单独下单 | 麦当劳官方限制，需搭配正价食品 |
| 点下单没反应 | 默认演示模式，需先取消勾选「演示模式」 |
| 端口不是 8808 | 被占用自动顺延，以打印地址为准 |

---

## 9. 作品能力边界（答辩可用）

| 能做 | 不能做 |
|---|---|
| 查真实门店、菜单、价格、优惠券 | **支付**（MCP 无此能力，只能跳官方页） |
| 用真实优惠规则算价 | 代扣、代付、存卡号 |
| 按结构求最低价、随机转盘 | 自动下单而不经用户确认 |
| 创建真实订单（需二次确认） | 预约单（界面未提供入口） |
| 官网链接跳转支付、取消订单 | 绕过门店/账号的地域与权限限制 |

**一句话**：点餐决策是我们解决的问题，支付交给官方渠道。
