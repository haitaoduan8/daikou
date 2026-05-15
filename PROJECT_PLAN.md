# 自动化操作逻辑（执行顺序与判定）

本文档只描述程序**实际怎么动**：先做什么、再做什么、何时停、用哪些字段。  
接口字段表、请求头、JSON 示例见 **`API_REFERENCE.md`**；源码见 **`ayidai_automation.py`**。

---

## 1. 会话（所有后续步骤的前置）

1. 得到 **Admin-Token**：用户直接填、或从 Cookie 里解析 `Admin-Token=`、或（可选）CDP 读浏览器 Cookie。
2. 调用 **`build_session(base, token, cookie)`**：创建 `requests.Session`，带上 `Authorization: Bearer …`、`device: 1`、`origin`、`referer`（委外贷后页），并把 Cookie 写入会话；保证存在 **Admin-Token** Cookie。
3. 之后每个请求由 **`api_post_json` / `api_get`** 补上 **`timestamp`**（毫秒字符串）；POST 还带 JSON `content-type`。

**无有效 Token → 不发起列表/计划/划扣。**

---

## 2. 拉齐列表（自动化核心：翻页）

**入口**：`fetch_all_orders(session, base, filter_template, page_size)`（GUI/CLI 共用）。

**单步请求**：

- `POST` `LIST_PATH`（`/prod-api/system/postLoan/wbOverdueOrder`）。
- 请求体 = `filter_template` 的全部键值 **再覆盖** `page`、`size`（`size` = 用户设的每页条数）。

**循环**：

1. `page` 从 **1** 开始。
2. 发请求，解析 JSON。
3. **若 `code != 200` → 抛错，终止。**
4. 读取 **`total`**（总条数）、**`data`**（本页数组）。
5. 把本页 `data` **追加**到内存中的总列表。
6. **停止条件**（满足任一即停）：`data` 为空，**或** 已累积条数 **≥ `total`**。
7. 否则 `page += 1`，回到步骤 2。

**产出**：合并后的订单行列表（可写 `orders_out.json`）。  
列表里每条的后台字段以后台为准；查计划时需要用户能提供该订单的 **`id`（订单主键）** 和 **`creditOrder` 字符串**（与列表/详情一致）。

---

## 3. 查还款计划（为划扣取「计划行 id」）

**入口**：`get_repayment_plans(session, base, order_id, credit_order, customer_id="", page=1, size=用户配置)`。

**请求**：

- `GET` `PLAN_PATH`（`/prod-api/system/sysCreditRepaymentPlan/postLoan/getOrderPlan`）。
- Query：**`id`** = 订单主键（即列表里的订单 `id`），**`creditOrder`**，`customerId` 默认可为空字符串，**`page`/`size`** 分页。

**判定**：

- **`code != 200` → 抛错。**
- 返回 **`data` 数组**：每一行是一条还款计划记录。

**与划扣的关系**：数组里每个元素的 **`id` 是「计划行 id」**——**划扣接口只用这个 id**，**不是**订单的 `id`。

工具在日志里会打出每期的 `repaymentNo`、`id`、`stateText`、`deductResult` 便于人工选期次。

---

## 4. 发起划扣

**入口**：`single_deduct(session, base, plan_row_id, dry_run)`。

**演练（`dry_run=True`）**：

- **不发 HTTP**；本地返回 `{"dry_run": true, "would_call": "GET …/single/deduct?id=…"}`。  
- GUI/CLI 仍可按配置写审计行（若有）。

**真实请求（`dry_run=False`）**：

- `GET` `DEDUCT_PATH`（`/prod-api/system/creditOrderDetailed/single/deduct`）。
- Query：仅 **`id` = `plan_row_id`（计划行 id）**。

**成功判定（工具侧）**：

- 响应 JSON 的 **`code == 200`** 为成功；否则为失败。
- 异常（网络、HTTP 非 2xx）→ 抛错；CLI 写审计 `ok: false` 等。

**审计（可选）**：在划扣前后把结构化字段 + 完整 `result` 追加到本地文件一行 JSON（与 HTTP 无关）。

---

## 5. 端到端顺序（人工配合点）

| 顺序 | 自动化做什么 | 人通常要提供什么 |
|------|----------------|------------------|
| 1 | 建 Session | Token / Cookie（及 `base`） |
| 2 | 按筛选 JSON 翻页直到拉齐 | 与浏览器一致的筛选文件 |
| 3 | 对单笔订单 GET 计划 | 订单 `id`、`creditOrder`（从列表或导出 JSON 看） |
| 4 | 对某一期 GET 划扣 | 该期在计划 `data` 里的 **`id`**（计划行 id） |

---

## 6. CLI 与上述逻辑的对应

- **`fetch-all`**：步骤 1 + 2，输出 JSON 文件。  
- **`plan`**：步骤 1 + 3，可打印/导出计划。  
- **`deduct`**：步骤 1 + 4（`plan-id` 即计划行 id）。  

无参或 `gui`：**同一套逻辑**，改在界面填参、后台线程执行。
