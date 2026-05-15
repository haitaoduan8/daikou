# 安易代委外助手 — HTTP 接口说明（入参 / 出参）

本文档描述本工具实际调用的 **3 个后端接口** 的请求方式、**通用请求头**、**入参字段**与**响应结构**（以当前 `ayidai_automation.py` 与 `filter.example.json` 为准）。  
后台若调整字段，请以浏览器「网络」面板中**真实请求**为准，并同步更新本文件与示例 JSON。

**默认服务根地址**（可改）：常量 `DEFAULT_BASE`，例如 `https://<host>`，下文记为 `{BASE}`。  
**完整 URL** = `{BASE}` + 下文「路径」（路径均以 `/prod-api/...` 开头）。

---

## 0. 通用约定

### 0.1 认证与 Cookie

| 方式 | 说明 |
|------|------|
| `Authorization` | `Bearer {Admin-Token}`，Token 与浏览器后台一致 |
| Cookie | 可选：浏览器复制的整段 Cookie；工具会解析并写入会话；若 Cookie 中含 `Admin-Token=` 也可作为 Token 来源 |
| `Admin-Token` | 工具会尽量在 Cookie 域下写入 `Admin-Token={token}`，与 Web 行为对齐 |

### 0.2 所有请求都会带上的头（由 `build_session` 设置）

| 请求头 | 值 | 说明 |
|--------|-----|------|
| `Authorization` | `Bearer <token>` | 必填（无 Token 时工具会拒绝业务请求） |
| `device` | `1` | 与后台约定 |
| `accept` | `application/json, text/plain, */*` | |
| `user-agent` | 默认与 **Chrome 147（macOS）** 一致；可用环境变量 **`AYIDAI_UA`** 覆盖 | 非浏览器 UA 可能被网关拦截 |
| `accept-language` | `zh-CN,zh;q=0.9` | 与浏览器一致 |
| `sec-fetch-dest` / `sec-fetch-mode` / `sec-fetch-site` | `empty` / `cors` / `same-origin` | 与同源 XHR 一致；若异常可设 **`AYIDAI_MINIMAL_HEADERS=1`** 去掉 |
| `origin` | `{BASE}` 去掉末尾 `/` | |
| `referer` | `{BASE}/afterLoanMag/outrepayment` | 与委外贷后页一致 |

### 0.3 由封装层每次请求追加 / 调整的头

| 请求头 | 何时设置 | 说明 |
|--------|----------|------|
| `timestamp` | 每次 `api_post_json` / `api_get` | **字符串**，毫秒时间戳，与 `_now_ms()` 一致 |
| `content-type` | 仅 POST JSON | `application/json;charset=UTF-8` |
| `content-type` | GET 前 | 从 session 中 **删除**（避免污染 GET） |

### 0.4 HTTP 状态与 JSON

- 使用 `requests`：`status` 非 2xx 时 **`raise_for_status()`**，**不会解析 body** 为业务 JSON（直接抛异常）。
- 2xx 时响应体按 **JSON 对象**解析；业务是否成功看下文 **`code` 字段**（工具侧以 `code == 200` 为成功）。

### 0.5 统一业务响应包（工具侧假设）

多数接口返回形如：

```json
{
  "code": 200,
  "msg": "…",
  "data": …,
  "total": 0
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | number | **200** 表示业务成功（与工具内判断一致）；非 200 时工具抛错或判失败 |
| `msg` | string | 提示信息，可选 |
| `data` | object / array / null | 业务载荷；列表接口为**数组**，计划接口为**数组** |
| `total` | number | **列表分页接口**使用：总条数；其它接口可能无此字段 |

**注意**：`msg`、`data` 内子字段以后台为准；下列「出参」仅列工具**明确读取**的字段。

---

## 1. 委外/逾期订单列表（分页查询）

### 1.1 基本信息

| 项 | 值 |
|----|-----|
| 方法 | `POST` |
| 路径 | `/prod-api/system/postLoan/wbOverdueOrder` |
| 完整 URL | `{BASE}/prod-api/system/postLoan/wbOverdueOrder` |
| Content-Type | `application/json;charset=UTF-8` |

### 1.2 请求体（JSON Object）

工具行为：`body = { ...filter_template, "page": <int>, "size": <int> }`，即在**筛选模板**上覆盖分页字段。  
筛选模板通常来自文件（如 `filter.example.json`），字段需与**浏览器同一接口**请求体一致。

#### 1.2.1 字段说明（与 `filter.example.json` 对齐）

| 字段 | 类型 | 示例 | 说明 |
|------|------|------|------|
| `linkNum` | string | `""` | 环节编号等，空表示不限 |
| `orderNo` | string | `""` | 订单号 |
| `phone` | string | `""` | 手机号 |
| `productId` | null / number | `null` | 产品 ID；`null` 表示不限（与后台一致时再改为具体 ID） |
| `applicationPath` | null / string | `null` | 申请路径 |
| `isCommittedRepay` | null / boolean | `null` | 是否承诺还款 |
| `followSituation` | null / string / number | `null` | 跟进情况 |
| `repayType` | null / number | `null` | 还款类型 |
| `lastFollowUpTimeStarting` | string | `""` | 最近跟进开始时间 |
| `lastFollowUpTimeEnd` | string | `""` | 最近跟进结束时间 |
| `loanTimeStarting` | string | `""` | 放款开始 |
| `loanTimeEnd` | string | `""` | 放款结束 |
| `repaymentTimeEnd` | string | `""` | 还款时间结束；空字符串表示不限 |
| `repaymentTimeStarting` | string | `""` | 还款时间开始；空字符串表示不限 |
| `endOutsourcingTime` | string | `""` | 委外结束时间 |
| `startOutsourcingTime` | string | `""` | 委外开始时间 |
| `overdueDays` | null / number | `null` | 逾期天数 |
| `followUserIds` | array | `[]` | 跟进人 ID 列表 |
| `overdueType` | number | `0` | 逾期类型 |
| `page` | number | 由工具覆盖 | **工具写入**：从 1 递增翻页 |
| `size` | number | 由工具覆盖 | **工具写入**：每页条数（GUI/CLI 可配） |
| `stopReminderStatus` | string | `""` | 停催状态 |
| `numberRange` | array | `[]` | 数字区间等 |
| `committedRepayStartTime` | string | `""` | 承诺还款开始 |
| `committedRepayEndTime` | string | `""` | 承诺还款结束 |
| `temporaryUserIds` | array | `[]` | 临时用户 ID |

**说明**：表中 `null`/`""`/`[]` 仅为示例默认值；实际筛选应与后台页面一致。若后台新增筛选字段，在 JSON 中补上即可。

#### 1.2.2 请求体示例（单页）

```json
{
  "linkNum": "",
  "orderNo": "",
  "phone": "",
  "productId": null,
  "applicationPath": null,
  "isCommittedRepay": null,
  "followSituation": null,
  "repayType": null,
  "lastFollowUpTimeStarting": "",
  "lastFollowUpTimeEnd": "",
  "loanTimeStarting": "",
  "loanTimeEnd": "",
  "repaymentTimeEnd": "",
  "repaymentTimeStarting": "",
  "endOutsourcingTime": "",
  "startOutsourcingTime": "",
  "overdueDays": null,
  "followUserIds": [],
  "overdueType": 0,
  "page": 1,
  "size": 50,
  "stopReminderStatus": "",
  "numberRange": [],
  "committedRepayStartTime": "",
  "committedRepayEndTime": "",
  "temporaryUserIds": []
}
```

### 1.3 响应体（JSON Object）

工具读取字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | number | 必须为 **200**，否则工具抛 `RuntimeError` |
| `total` | number | 总记录数；用于判断是否停止翻页 |
| `data` | array | **当前页**订单行列表；元素为对象，结构由后台定义 |

#### 1.3.1 工具对 `data` 元素的隐含依赖（还款计划 / 人工填参）

列表行完整 schema 未在仓库中固化。使用「还款计划」时，用户需在界面填写：

- **订单主键**：对应后台订单 ID，还款计划接口 Query 参数名为 **`id`**。列表行里常见为 **`orderId`**，部分环境为 **`id`**；一键处理会依次读取 **`id`**，若无则读 **`orderId`**。
- **creditOrder**：字符串，须与列表或详情中的 **`creditOrder`** 字段一致（具体在哪个字段以实际 JSON 为准）。

建议在导出 `orders_out.json` 后，用实际一条记录确认字段名（常见会包含 `orderId` 或 `id`、`creditOrder` 等）。

#### 1.3.2 响应示例（结构示意，数据为虚构）

```json
{
  "code": 200,
  "msg": "success",
  "total": 128,
  "data": [
    {
      "orderId": 123456,
      "creditOrder": "CR2026xxxx",
      "orderNo": "ONxxxx",
      "phone": "13800138000"
    }
  ]
}
```

### 1.4 工具侧翻页逻辑

1. `page` 从 1 开始，每次 `size` 取 GUI/CLI 设定值（请求体里会覆盖筛选模板中的 `page` / `size`）。  
2. 将返回的 `data` 追加到内存列表。  
3. 若 **`total` 为大于 0 的整数**：`data` 为空，或已累积条数 `>= total`，停止。  
4. 若 **`total` 缺失或为 0**：不在第一页就停；以 **`data` 为空** 或 **本页条数 `< size`** 作为末页条件（避免 `total=0` 时误只拉一页）。  
5. 否则 `page += 1` 继续 POST（带安全上限页数，防止异常死循环）。

**条数与「案件库」不一致时**：先核对本工具使用的请求体是否与后台列表**同一筛选**（如日期、产品、跟进人等）；`wbOverdueOrder` 与别的菜单的「全量案件」也可能不是同一数据源。

---

## 2. 还款计划查询

### 2.1 基本信息

| 项 | 值 |
|----|-----|
| 方法 | `GET` |
| 路径 | `/prod-api/system/sysCreditRepaymentPlan/postLoan/getOrderPlan` |
| 完整 URL | `{BASE}/prod-api/system/sysCreditRepaymentPlan/postLoan/getOrderPlan` |
| Query | 见下表 |

### 2.2 查询参数（Query String）

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | number | 是 | **订单 ID**（与列表行中的 `id` 或 `orderId` / GUI「订单 orderId」一致） |
| `creditOrder` | string | 是 | 授信订单号，与列表字段一致 |
| `customerId` | string | 否 | 客户 ID；工具默认传 **`""`**（空字符串） |
| `page` | number | 否 | 页码；工具默认 **1** |
| `size` | number | 否 | 每页条数；工具默认 **100**（GUI 可改） |

**示例 URL**（仅示意编码）：

```http
GET {BASE}/prod-api/system/sysCreditRepaymentPlan/postLoan/getOrderPlan?id=123456&creditOrder=CR2026xxxx&customerId=&page=1&size=100
```

### 2.3 响应体（JSON Object）

工具读取：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | number | **200** 为成功 |
| `data` | array | **还款计划行**列表；**每条计划行的 `id` 为「划扣」接口使用的 `id`** |

#### 2.3.1 计划行字段（工具日志中会打印）

工具在 GUI/CLI 中显式使用以下字段做展示或逻辑（其它字段原样保存在导出 JSON 中）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | number | **还款计划行主键**；**发起划扣**时作为唯一业务 ID |
| `repaymentNo` | string / number | 期次等（展示用） |
| `stateText` | string | 状态文案（展示用） |
| `deductResult` | string / object | 划扣结果；**非空不代表已成功**（如「余额不足」仍应可再划扣）。工具仅当文案含成功类关键字或 `stateText` 含已还/结清等时才跳过该行。 |

#### 2.3.2 响应示例（结构示意）

```json
{
  "code": 200,
  "msg": "success",
  "data": [
    {
      "id": 987654321,
      "repaymentNo": "1",
      "stateText": "待还",
      "deductResult": ""
    }
  ]
}
```

---

## 3. 单笔发起划扣

### 3.1 基本信息

| 项 | 值 |
|----|-----|
| 方法 | `GET` |
| 路径 | `/prod-api/system/creditOrderDetailed/single/deduct` |
| 完整 URL | `{BASE}/prod-api/system/creditOrderDetailed/single/deduct` |

### 3.1.1 与前端「两次确定」一致（工具默认行为）

Chrome DevTools 可见：同一 URL、同一 `id` 会连续出现 **两次** `GET single/deduct`；首次响应可能为 **`code` 500**、`msg` 含 **「不允许重复提交」**，第二次常为 **`code` 200**（如「扣款进行中」）。  
工具内 `single_deduct`：**若首次 `code != 200`**，在短暂间隔后 **自动再请求一次**（与双击确定对齐）。首次已是 200 则不再发第二次。

| 环境变量 | 说明 |
|----------|------|
| `AYIDAI_DEDUCT_NO_RETRY=1` | 只发 **一次** GET（调试用） |
| `AYIDAI_DEDUCT_RETRY_DELAY` | 两次 GET 间隔秒数，默认 **`0.35`** |
| `AYIDAI_DEDUCT_SINGLE_IF_OK=1` | 若 **首次** 已 `code==200` 则 **不再** 发第二次（旧行为；默认关闭，与页面「两次确定」一致） |
| `AYIDAI_UA` | 覆盖默认 Chrome User-Agent |

默认：**总是连续两次相同 GET**，再在两次结果中 **优先采用 `code==200` 的那一次**（若仅首次为 200，则返回首次并附带 `_deduct_second_attempt`）。响应中可能含 **`_deduct_first_attempt`** / **`_deduct_second_attempt`** 便于审计。

**Cookie**：浏览器里除 `Admin-Token` 外常有 **`acw_tc`、`SERVERID`** 等，建议整段复制进工具 Cookie 框，减少风控差异。

### 3.2 查询参数（Query String）

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | number | 是 | **还款计划行的 `id`**（来自 §2 `data[]`），**不是**订单 `id` |

**示例**：

```http
GET {BASE}/prod-api/system/creditOrderDetailed/single/deduct?id=987654321
```

### 3.3 响应体（JSON Object）

工具行为：

- **非演练**：至多两次相同 GET；以**最后一次**解析结果为主；成功判定为 **`res.get("code") == 200`**（`res` 可能含 `_deduct_first_attempt`）。
- **演练（dry-run）**：**不发起 HTTP**；工具本地返回：

```json
{
  "dry_run": true,
  "would_call": "GET /prod-api/system/creditOrderDetailed/single/deduct?id=<plan_row_id>"
}
```

#### 3.3.1 成功判定（真实请求）

| 条件 | 工具认为 |
|------|----------|
| `code == 200` | 成功（GUI 弹成功、CLI exit 0、审计 `ok: true`） |
| `code != 200` 或缺省 | 失败（审计 `ok: false`，CLI exit 1） |

后台可能另有 `msg`、`data` 等字段；工具会把**完整 `res`** 写入审计字段 `result`（见 §4）。

#### 3.3.2 响应示例（虚构）

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": null
}
```

---

## 4. 代扣审计文件（非接口，本地落盘）

划扣时工具可向本地文件**追加一行 JSON**（与 HTTP 无关，便于对账）。

### 4.1 每条审计记录常见字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `time` | string | ISO 时间（本地时区） |
| `action` | string | 固定 `single_deduct` |
| `endpoint` | string | 固定 `/prod-api/system/creditOrderDetailed/single/deduct` |
| `plan_row_id` | number | 计划行 `id` |
| `dry_run` | boolean | 是否演练 |
| `base` | string | 当时使用的 API 根 URL |
| `order_id` | number | 可选，GUI/CLI 填入的订单 id |
| `credit_order` | string | 可选，审计用 |
| `ok` | boolean | 是否成功（演练为 true） |
| `result` | object | 完整业务响应或本地 dry_run 对象 |
| `error` | string | 请求异常时 `repr(e)` |

---

## 5. 与 CLI / GUI 的对应关系

| 能力 | CLI | GUI |
|------|-----|-----|
| 列表 | `fetch-all --filter <json> --out ... --page-size ...` | 「拉取列表」Tab |
| 计划 | `plan --order-id --credit-order --size --out` | 「还款计划」Tab |
| 划扣 | `deduct --plan-id [--dry-run] [--audit-log]` | 「发起划扣」Tab |

环境变量常用：`ADMIN_TOKEN`、`AYIDAI_BASE`、`AYIDAI_COOKIE` 等（见 `--help`）。

---

## 6. 修订说明

- 新增/变更筛选字段：更新 **`filter.example.json`** 与本文件 **§1.2**。  
- 后台改路径或参数名：更新 **`ayidai_automation.py` 中常量**与本文件路径表。  
- 计划行或列表行结构变化：在 **§1.3.1 / §2.3** 补充实际样例（可从浏览器复制响应脱敏后粘贴）。
