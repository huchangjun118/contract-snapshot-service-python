# 接口契约快照与兼容性回归

本文档说明归一化语义、兼容性判定规则、配置来源与优先级、采集范围、
路径通配语法、持久化行为、批准与回滚流程以及失败状态。
所有规则都是**确定的**：相同输入在任何时间、任何进程下得到相同结论。

## 1. 采集与归一化语义

采集入口既接受**已解析的负载**，也接受**原始请求/响应报文**。
完整流程为：范围/采样判定 → 解析报文 → 归一化 → 提取结构 schema →
持久化版本 → 与当前基线比较并**固化判定**。

### 1.1 原始报文解析

- 支持完整 HTTP 报文（请求行/状态行 + 头部 + 空行 + 消息体），
  以 `\r\n\r\n` 或 `\n\n` 分隔；根据 `Content-Type` 选择解析器，
  当前支持 `application/json`（含 `+json` 后缀与 `charset` 参数）。
- 没有报文分隔、或首行不是合法 HTTP 起始行时，整段按**裸 JSON** 解析。
- 报文不是合法 UTF-8、为空、或 JSON 解析失败 → 失败类别
  `unparseable_body`（HTTP 422）；错误信息携带行列号，类别稳定、可区分。
- `Content-Type` 明确但不支持（如 `text/plain`）→
  `unsupported_media_type`（HTTP 415）。
- 解析失败时该次采集**不落盘、不覆盖/污染基线、不写判定记录**，
  也不会被记成「无变更」。

### 1.2 归一化

归一化保证以下内容**不引起快照变化**：

| 因素 | 处理 |
| --- | --- |
| 字段顺序 | 对象按键名排序后比较 |
| 无关空白 | 字符串去除首尾空白 |
| 易变字段（支持路径通配） | 取值替换为占位符 `<volatile>`，结构保留 |
| 忽略字段（支持路径通配） | 整棵子树不进入快照 |
| 数组顺序 | 默认按元素规范序列化排序（可用 `ignore_array_order=false` 关闭） |
| 浮点表示差异 | `1.0` 与 `1` 视为同一取值 |

版本身份 = **接口名 + 结构 schema 内容哈希 + 归一化规则指纹**。
因此同一份契约在同一套规则下，不同时间、不同进程采集得到同一个
版本号；规则变更后得到新版本身份。归一化与哈希均为纯函数，
不依赖哈希随机化、字典插入序等进程相关因素。

## 2. 路径通配语法

忽略字段（`ignore_paths`）与易变字段（`volatile_paths`）都支持
**嵌套路径与通配符**，按「根 → 目标字段」的段序列匹配：

- 段以 `.` 分隔，例如 `response.data.debug` 只忽略该深层字段，
  不影响其它层级的同名 `debug`；
- `*` 匹配恰好**一个**字段段（不能跨 `.`），如 `data.*.secret`；
- `**` 匹配**零个或多个**字段段（可跨层），如 `**.token`；
- 不含 `.` 与通配符的**裸名**（如 `timestamp`）匹配任意深度下的
  同名字段，等价于 `**.timestamp`；
- 数组下标不参与路径匹配，同一字段路径下的数组元素沿用该字段路径。

匹配是确定性的：同一份契约在不同时间与不同进程下采集，得到
**可比较的同一份快照**。

## 3. 兼容性判定规则与配置变更语义

比较**当前基线 schema** 与**候选 schema**，结论三选一：
`identical` / `compatible` / `breaking`。

1. 完全一致 → `identical`。
2. **新增字段** → `compatible`（视为新增可选字段）。
3. **删除字段** → `breaking`。
4. 标量类型演进：
   - 放宽（新类型集合 ⊇ 旧集合，含 `integer → number`）→ `compatible`；
   - 收紧（如 `number → integer`）、部分重叠或不相交（如 `string → boolean`）
     → `breaking`。
5. 结构种类变化（object ↔ array ↔ scalar）→ `breaking`。
6. 对象字段与数组元素递归适用以上规则；任一子节点破坏则整体 `breaking`。

### 3.1 配置变更不得让破坏降级（关键语义）

判定**永远基于两份已固化的结构 schema 直接比较**，与它们生成时使用的
归一化规则是不是同一套**无关**。因此：

- 无论采集侧配置怎么改，只要候选相对当前基线**删除了字段**或
  **收紧了类型**，结论仍然必须是 `breaking`；
- 不会因为候选与基线的规则指纹不同就把比较降级为 `compatible`；
- 采集结果中的 `rules_match` 字段仅标注双方规则是否同套（便于排查），
  **不参与判定**。

### 3.2 配置变更不影响历史

- 每次采集的判定在当时就固化进 `judgments` 表（只追加，不更新）；
- 之后再改配置，**不会重新解释、也不会改写**已落库版本与历史判定；
- 配置变更只影响**之后**的采集与判定。查询历史判定见
  `GET /judgment/{endpoint}` 或 `ContractService.get_last_judgment`。

**验证方法**：先以规则 A 采集并批准含字段 `b` 的基线；再用「忽略 `b`」
的规则 B 采集仍带 `b` 的负载，候选 schema 中 `b` 消失，
结论必须仍为 `breaking`（见 `tests/test_config_change_compat.py`）。

## 4. 批准、基线与回滚

- 采集产生的版本默认**未批准**，不参与基线，也不影响后续判定。
- 只有显式 `approve` 的版本才提升为基线；未批准采集永不覆盖基线。
- `rollback` 可把基线回滚到任意**已批准**的历史版本；回滚到未批准
  版本返回 `not_approved`。
- 批准与回滚互斥执行，并发时结果确定（后提交者生效）；任一时刻读到
  的基线都是某个已提交的完整版本，不会出现新旧字段混合。
- 存储写入以事务为单位，不存在写了一半的中间产物；基线读取按主键
  查询，版本列表带 `limit`，性能不随历史版本数量线性退化。

## 5. 配置来源与读取优先级

优先级从高到低：

1. **进程环境变量**（`CONTRACT_SNAPSHOT_*`）；
2. **配置文件**（JSON；路径由环境变量 `CONTRACT_SNAPSHOT_CONFIG`
   或 `load_config(config_path=...)` 指定）；
3. **代码默认值**。

环境变量只覆盖被显式设置的键；列表型变量（逗号分隔）一旦设置即为
完整覆盖，不与配置文件合并。服务启动时加载一次配置（`main.py`）。

配置文件示例：

```json
{
  "sampling_rate": 1.0,
  "include_endpoints": ["/v1/*"],
  "exclude_endpoints": ["/v1/health"],
  "rules": {
    "ignore_paths": ["response.data.debug", "**.internal"],
    "volatile_paths": ["**.token", "timestamp", "request_id"],
    "ignore_array_order": true
  }
}
```

| 环境变量 | 对应配置键 | 说明 |
| --- | --- | --- |
| `CONTRACT_SNAPSHOT_CONFIG` | — | 配置文件路径 |
| `CONTRACT_SNAPSHOT_SAMPLING_RATE` | `sampling_rate` | `[0.0,1.0]`，`0` 关闭采集 |
| `CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS` | `include_endpoints` | 逗号分隔的采集白名单 |
| `CONTRACT_SNAPSHOT_EXCLUDE_ENDPOINTS` | `exclude_endpoints` | 逗号分隔的采集黑名单 |
| `CONTRACT_SNAPSHOT_IGNORE_PATHS` | `rules.ignore_paths` | 逗号分隔的忽略路径 |
| `CONTRACT_SNAPSHOT_VOLATILE_PATHS` | `rules.volatile_paths` | 逗号分隔的易变路径 |
| `CONTRACT_SNAPSHOT_IGNORE_ARRAY_ORDER` | `rules.ignore_array_order` | `true`/`false` |

显式（或环境变量）指定的配置文件不存在或无法解析时，启动即报
`invalid_config`；未指定文件时静默使用默认值。

### 5.1 采集范围

- `include_endpoints` 为白名单、`exclude_endpoints` 为黑名单，
  使用 `fnmatch` 通配（`*` 可跨 `/`，另支持 `?` 与字符集）；
- 白名单为空表示不限制；黑名单命中优先拒绝；
- 不在范围内的接口**不解析报文、不产生任何版本、不写判定**，
  返回 `sampled=false, in_scope=false`；
- `sampling_rate=0`（采集关闭）时，采集动作直接返回
  `sampled=false, in_scope=true` 且不写任何数据（不读/解析报文）。

## 6. 重启后的持久化行为

- 默认使用 **SQLite 单文件库**，文件库启用 WAL 支持并发读写；
- **默认落盘位置**：工作目录下的
  `./.contract_snapshot/contracts.db`（目录自动创建）；
  可用环境变量 `CONTRACT_SNAPSHOT_DATA_DIR` 改默认目录，
  或用 `CONTRACT_SNAPSHOT_DB` 指定完整库路径；
- 未显式配置持久化位置时默认即落盘：**重启进程后再次查询，
  仍能看到重启前采集的版本、已批准的基线与历史判定**；
- 显式传 `:memory:` 时退化为纯内存库（测试用），重启后数据消失。

验证方法：`tests/test_persistence.py` 用两个独立子进程分别写入
（采集 + 批准）与重开读取，确认基线版本跨进程一致。

## 7. 失败状态

| kind | HTTP | 含义 |
| --- | --- | --- |
| `unparseable_body` | 422 | 原始报文无法解析，不落盘、不污染基线 |
| `unsupported_media_type` | 415 | 报文 Content-Type 不支持 |
| `store_unavailable` | 503 | 存储暂时不可用 |
| `version_not_found` | 404 | 目标版本不存在 |
| `not_approved` | 409 | 版本未批准，不能作为基线/回滚目标 |
| `conflict` | 409 | 并发冲突，可重试 |
| `invalid_config` | 400 | 配置非法 |

任何失败都不会被当成「无变更」，也不会写入半成品。

## 8. HTTP 接口

- `POST /capture` `{endpoint, body}` → 用**已解析负载**采集；
- `POST /capture/raw/{endpoint}` → 直接发送**原始请求/响应报文**
  （完整 HTTP 报文或裸 JSON），无法解析返回 422/415；
- `POST /approve` `{version_id}` → 批准并提升为基线；
- `POST /rollback` `{endpoint, version_id}` → 回滚基线；
- `GET /baseline/{endpoint}` → 查询当前基线；
- `GET /versions/{endpoint}?limit=N` → 列出历史版本；
- `GET /judgment/{endpoint}` → 查询最近一次固化的采集判定。

采集响应字段：

| 字段 | 说明 |
| --- | --- |
| `verdict` | `identical`/`compatible`/`breaking`；未采样时为 `null` |
| `sampled` | 是否在范围内且采样命中（未命中不落盘） |
| `in_scope` | 接口是否在采集范围内 |
| `rules_match` | 候选与基线规则是否同套（仅标注，不影响判定） |
| `baseline_version_id` | 比较时使用的基线（可能为空） |
