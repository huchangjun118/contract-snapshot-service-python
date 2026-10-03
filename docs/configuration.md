# 配置项

配置控制采集范围与归一化规则。配置变更**不会**破坏已有基线的可比性，
也**不会**让历史判定结果被重新解释。

## 配置加载顺序

1. 环境变量 `CONTRACT_SNAPSHOT_CONFIG` 指定的 JSON 文件。
2. 当前目录下的 `contracts.config.json`（若存在）。
3. 内置默认值。

数据目录可通过环境变量 `CONTRACT_SNAPSHOT_DATA_DIR` 覆盖。

## 配置示例

```json
{
  "data_dir": "./contracts_data",
  "collection": {
    "enabled": true,
    "sample_rate": 1.0,
    "include_endpoints": ["*"],
    "exclude_endpoints": [],
    "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"]
  },
  "normalization": {
    "volatile_fields": ["timestamp", "request_id", "cursor"],
    "volatile_query_params": ["cursor", "timestamp"],
    "strip_headers": ["x-request-id", "date", "etag"],
    "normalize_path_ids": true
  }
}
```

## 采集配置（collection）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用采集。`false` 时所有采集返回 `ignored`。 |
| `sample_rate` | float | `1.0` | 采样率，`0.0`–`1.0`。`1.0` = 全量采集（关闭采样），`0.0` = 不采集。采样按契约确定性执行：同一份契约要么总是被采集，要么总是被跳过。 |
| `include_endpoints` | list[string] | `["*"]` | 采集范围（glob 模式，匹配 `METHOD:/path`）。`*` 表示全部。 |
| `exclude_endpoints` | list[string] | `[]` | 排除范围（glob 模式）。排除优先于包含。 |
| `methods` | list[string] | `["GET","POST","PUT","PATCH","DELETE"]` | 允许采集的 HTTP 方法。 |

### 采集范围示例

只采集 `POST /api/orders`：

```json
{"include_endpoints": ["POST:/api/orders"]}
```

排除 `GET /api/health`：

```json
{"exclude_endpoints": ["GET:/api/health"]}
```

采集所有 `/api/v2/` 路径下的接口：

```json
{"include_endpoints": ["*:/api/v2/*"]}
```

## 归一化配置（normalization）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `volatile_fields` | list[string] | 见下 | 易变字段名或 glob 路径，匹配的字段从 body 中剥离。 |
| `volatile_query_params` | list[string] | 见下 | 易变查询参数名，匹配的参数从 query 中剥离。 |
| `strip_headers` | list[string] | 见下 | 不进入契约的头部（当前仅 `Content-Type` 被保留）。 |
| `normalize_path_ids` | bool | `true` | 是否将路径中的数字/UUID/长十六进制段折叠为 `{id}`。 |

### 默认易变字段

```
timestamp, request_id, requestId, trace_id, traceId, span_id, spanId,
correlation_id, correlationId, nonce,
cursor, next_cursor, nextCursor, previous_cursor, previousCursor,
page_token, pageToken, next_page_token, nextPageToken, offset
```

### 默认易变查询参数

```
cursor, next_cursor, nextCursor, page_token, pageToken,
timestamp, request_id, trace_id, nonce
```

### 匹配规则

- 精确键名：`timestamp` 匹配任意层级的 `timestamp` 键。
- glob 路径：`*.trace_id` 匹配任意嵌套层级的 `trace_id`（不匹配顶层）。
- glob 键名：`*id` 匹配任意以 `id` 结尾的键名。

## 配置哈希

每个采集的版本记录当时的配置哈希（`config_hash`，16 字符）。
可通过 `GET /api/contracts/config` 查看当前配置的哈希与来源。

## 配置变更的影响

- 配置变更只影响**之后**的采集。
- 已存储的版本文件是不可变的，不会被重新归一化或改写。
- 历史判定结果不会因配置变更而改变（判定只读取已存储的结构）。
- 因此可以安全地调整采集范围或归一化规则，而不影响已有基线的可比性。
