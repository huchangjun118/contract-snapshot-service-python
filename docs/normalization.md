# 归一化语义

归一化把一次真实的请求/响应采集（raw capture）转换成一份**可比较的契约快照**。
归一化是确定性的：同一份契约在不同时间、不同进程下采集，得到的快照完全相同。

## 设计目标

- 字段顺序、无关空白不得引起快照变化。
- 时间戳、请求标识、分页游标等**易变字段**不得引起快照变化。
- 资源 id（路径中的数字/UUID 段、响应体中的 `id` 值）不影响契约结构。
- 归一化只看**结构**（shape），不看具体值。

## 归一化步骤

### 1. 请求体与响应体解析

- 已经是 JSON 原生类型（dict/list）的 body 直接使用。
- 字符串/字节类型的 body，仅当 `Content-Type` 指示 JSON（包含 `json`，如
  `application/json`、`application/vnd.api+json`）时才解析。
- 解析失败产生**可区分的失败状态**（`parse_error`），不会被当成"无变更"。
- 非 JSON 的文本 body 保留为字符串，其结构类型为 `string`。
- 空 body（`null`、空字符串）归一化为 `null`。

### 2. 易变字段剥离

递归遍历 body，删除名称匹配易变字段模式的键。默认模式包括：

- 时间戳类：`timestamp`
- 请求标识类：`request_id`、`requestId`、`trace_id`、`traceId`、`span_id`、
  `spanId`、`correlation_id`、`correlationId`、`nonce`
- 分页游标类：`cursor`、`next_cursor`、`nextCursor`、`previous_cursor`、
  `previousCursor`、`page_token`、`pageToken`、`next_page_token`、
  `nextPageToken`、`offset`

匹配规则支持：

- 精确键名匹配（如 `timestamp` 匹配任意层级的 `timestamp` 键）。
- glob 路径匹配（如 `*.trace_id` 匹配任意嵌套层级的 `trace_id`，但不匹配顶层）。

### 3. 查询参数归一化

- 解析 query string，按键名排序。
- 剥离易变查询参数（`cursor`、`timestamp`、`request_id` 等）。
- 剩余参数保留为 `键 → 值` 映射（值取第一个）。

### 4. 路径归一化

路径中的资源标识段被折叠为 `{id}`：

- 纯数字段：`/items/123` → `/items/{id}`
- UUID 段：`/items/550e8400-e29b-41d4-a716-446655440000` → `/items/{id}`
- 长十六进制段（≥16 字符，如 Mongo ObjectId）：`/items/507f1f77bcf86cd799439011` → `/items/{id}`

可通过 `normalization.normalize_path_ids = false` 关闭。

### 5. 头部处理

- 只保留 `Content-Type`（去掉参数，如 `application/json; charset=utf-8` →
  `application/json`）。
- 其余头部（`X-Request-Id`、`Date`、`ETag` 等）不进入契约结构。

### 6. 规范化 JSON

最终快照被序列化为**紧凑 JSON**（无空白），字典键**递归排序**。
这保证了字段顺序与无关空白不影响指纹。

## 结构（Shape）与指纹

归一化后的快照进一步提取**结构**（shape），结构只描述类型，不描述值：

| JSON 值 | 结构类型 |
|---------|----------|
| `null` | `null` |
| `true`/`false` | `boolean` |
| 整数/浮点 | `number` |
| 字符串 | `string` |
| 数组 | `array`（元素结构为所有元素结构的合并） |
| 对象 | `object`（属性名为键，属性值为子结构） |

数组的元素结构取所有元素结构的**合并**：

- 对象合并：属性取并集，共有属性递归合并。
- 标量合并：类型取并集；若类型不一致，记为 `mixed`（如 `[1, "x"]` 的元素类型为
  `number|string`）。
- 空数组：元素类型为 `unknown`。

**指纹（version_id）= 结构的 SHA-256 哈希。** 因此：

- 同一份契约（结构相同）在不同时间/进程采集 → 同一指纹 → 去重为一个版本。
- 结构不同 → 不同指纹 → 不同版本。

## 配置变更的影响

- 每个采集的版本记录当时的配置哈希（`config_hash`）。
- 配置变更只影响**之后**的采集；已存储的版本是不可变的，不会被重新解释或改写。
- 因此历史判定结果不会因配置变更而改变。
