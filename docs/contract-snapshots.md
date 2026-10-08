# 接口契约快照与兼容性回归

本文档说明归一化语义、兼容性判定规则、批准与回滚流程、配置项与失败状态。
所有规则都是**确定的**：相同输入在任何时间、任何进程下得到相同结论。

## 1. 采集与归一化语义

从真实请求/响应体采集契约快照，流程为：解析 → 归一化 → 提取结构 schema → 持久化。

归一化保证以下内容**不引起快照变化**：

| 因素 | 处理 |
| --- | --- |
| 字段顺序 | 对象按键名排序后比较 |
| 无关空白 | 字符串去除首尾空白 |
| 时间戳、请求标识、分页游标等易变字段 | 取值替换为占位符 `<volatile>`，结构保留 |
| 数组顺序 | 默认按元素规范序列化排序（可用 `ignore_array_order=False` 关闭） |
| 浮点表示差异 | `1.0` 与 `1` 视为同一取值 |

版本身份取自**结构 schema 的内容哈希**（不含具体取值），因此同一契约在不同
时间、不同进程下采集得到同一个版本号。归一化与哈希均为纯函数，不依赖哈希
随机化、字典插入序等进程相关因素。

## 2. 兼容性判定规则

比较基线 schema 与候选 schema，结论三选一：`identical` / `compatible` / `breaking`。

1. 完全一致 → `identical`。
2. **新增字段** → `compatible`（视为新增可选字段）。
3. **删除字段** → `breaking`。
4. 标量类型演进：
   - 放宽（新类型集合 ⊇ 旧集合，含 `integer → number`）→ `compatible`；
   - 收紧（如 `number → integer`）、部分重叠或不相交（如 `string → boolean`）→ `breaking`。
5. 结构种类变化（object ↔ array ↔ scalar）→ `breaking`。
6. 对象字段与数组元素递归适用以上规则；任一子节点破坏则整体 `breaking`。

## 3. 批准与基线

- 采集产生的版本默认**未批准**，不参与基线，也不影响后续判定。
- 只有显式 `approve` 的版本才提升为基线。
- 未批准的采集永远不会覆盖已有基线。

## 4. 回滚

- `rollback` 可把基线回滚到任意**已批准**的历史版本；回滚到未批准版本返回
  `not_approved`。
- 批准与回滚互斥执行，并发时结果确定（后提交者生效）；任一时刻读到的基线
  都是某个已提交的完整版本，不会出现新旧字段混合。
- 存储写入以事务为单位，不存在写了一半的中间产物；基线读取按主键查询，
  版本列表带 `limit`，性能不随历史版本数量线性退化。

## 5. 配置项

### 5.1 配置来源与读取优先级

配置按以下优先级合并（高优先级覆盖低优先级）：

1. **环境变量**（`CONTRACT_SNAPSHOT_*`）；
2. **配置文件**（TOML；路径由 `CONTRACT_SNAPSHOT_CONFIG` 指定，
   缺省查找当前目录的 `contract-snapshot.toml`）；
3. **代码默认值**。

| 配置 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `sampling_rate` | `CONTRACT_SNAPSHOT_SAMPLING_RATE` | `1.0` | 采样率 `[0.0, 1.0]`，`0` 表示关闭采集 |
| `include_endpoints` | `CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS` | 空（全部采集） | 采集范围，见 5.2 |
| `db_path` | `CONTRACT_SNAPSHOT_DB` | `~/.contract-snapshot/snapshots.db` | 持久化位置，见 5.4 |
| `rules.ignore_paths` | `CONTRACT_SNAPSHOT_IGNORE_PATHS` | 空 | 完全不进入快照的字段路径模式 |
| `rules.volatile_paths` | `CONTRACT_SNAPSHOT_VOLATILE_PATHS` | 时间戳/请求标识/游标等 | 取值被抹除、结构保留的字段路径模式 |
| `rules.ignore_array_order` | `CONTRACT_SNAPSHOT_IGNORE_ARRAY_ORDER` | `true` | 数组是否顺序不敏感 |

环境变量中的列表值用逗号分隔。配置文件示例：

```toml
sampling_rate = 1.0
include_endpoints = ["/api/*"]
db_path = "/var/lib/contract-snapshot/snapshots.db"

[rules]
ignore_paths = ["data.*.debug"]
volatile_paths = ["**.token", "timestamp"]
ignore_array_order = true
```

### 5.2 采集范围

`include_endpoints` 是 fnmatch 风格的接口路径模式列表（如 `/api/*`）：

- 列表为空：采集全部接口；
- 列表非空：只有命中任一模式的接口才会采集，**不在范围内的接口不产生
  任何版本**，采集动作返回 `sampled=false` 且不写任何数据；
- `sampling_rate=0` 关闭采集时同样返回 `sampled=false` 且不落盘。

### 5.3 路径通配语法

`ignore_paths` 与 `volatile_paths` 按**路径模式**匹配，而不是只按字段名：

- 模式以 `.` 分段，从根开始锚定：`data.user.token` 只匹配根下
  `data.user` 里的 `token`；
- `*` 匹配恰好一层：`data.*.token` 匹配 `data` 下任意一层对象里的 `token`；
- `**` 匹配任意层数（含零层）：`**.token` 匹配任意深度的 `token`；
- 不含 `.` 的裸字段名（如 `debug`）匹配任意深度下的同名字段，
  与早期按字段名整体匹配的行为兼容；
- 数组不增加路径段：`{"items": [{"id": 1}]}` 中 `id` 的路径是 `items.id`。

匹配是纯函数：同一份契约在不同时间、不同进程下采集得到可比较的
同一份快照。

### 5.4 持久化与重启

快照版本与基线指针存储在 SQLite 单文件中（WAL 模式）。未显式配置
`db_path` 时使用默认落盘位置 `~/.contract-snapshot/snapshots.db`
（目录自动创建），**默认配置下重启服务后仍能查到重启前采集的版本与
已批准的基线**。同一契约重启后再次采集得到同一个版本号，判定为
`identical`。

### 5.5 配置变更后的判定语义

每个版本落盘时记录**规则指纹与规则本体**，已落库版本的判定依据不会被
后续配置变更改写。配置变更后采集的候选与历史基线比较时：

- 用**基线落库时记录的规则**重新归一化候选负载，再与基线 schema 比较；
- 因此只要候选相对基线**删除了字段或收紧了类型**，无论采集侧配置怎么改，
  判定结果仍然是 `breaking`，不会降级为 `compatible` 或 `identical`；
- 历史判定结论不被重新解释，配置只影响之后的采集与判定。

验证方法：`pytest tests/test_failures.py -k config_change` 覆盖了
「配置变更后删除字段仍判破坏」「类型收紧仍判破坏」「历史基线不被改写」。

## 6. 失败状态

| kind | HTTP | 含义 |
| --- | --- | --- |
| `unparseable_body` | 422 | 请求/响应体无法解析，不落盘、不污染基线 |
| `store_unavailable` | 503 | 存储暂时不可用 |
| `version_not_found` | 404 | 目标版本不存在 |
| `not_approved` | 409 | 版本未批准，不能作为基线/回滚目标 |
| `conflict` | 409 | 并发冲突，可重试 |
| `invalid_config` | 400 | 配置非法 |

任何失败都不会被当成「无变更」，也不会写入半成品。

## 7. HTTP 接口

- `POST /capture` `{endpoint, body}` 或 `{endpoint, raw_body}` → 采集并返回
  与基线的判定结论。`body` 是已解析的负载；`raw_body` 是原始报文文本，
  由服务端解析，无法解析时返回 422 `unparseable_body`，该次采集不落盘、
  不覆盖基线、不被记成无变更
- `POST /approve` `{version_id}` → 批准并提升为基线
- `POST /rollback` `{endpoint, version_id}` → 回滚基线
- `GET /baseline/{endpoint}` → 查询当前基线
- `GET /versions/{endpoint}?limit=N` → 列出历史版本
