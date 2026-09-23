# 实验室仪器数据接收 API

基于 **FastAPI + SQLite** 的仪器批量测量包接收服务。仪器按设备上报带序号的测量包，服务提供：

- **幂等去重**：每个包携带幂等键，重试 / 重发不会产生重复数据或重复告警事件；
- **乱序缓冲**：序号不连续的包先暂存，缺口补齐后**按序连续提交**（告警也按该提交顺序评估）；
- **缺包作废**：确认丢失的连续序号段（从 `next_expected` 开始的缺口）可登记作废，推进顺序前沿并立即排水随后滞留的缓冲包；作废与摄入共用串行锁与事务、携带幂等键可安全重试，作废序号不伪装成测量包——不产生数据、不参与告警、不进入导出；
- **可观测接口**：查询每台设备的缺口、已提交序号、重复包与作废记录；
- **窗口聚合**：按事件时间（`sent_at`）把已提交测量分入半开窗口，输出 count / min / max / mean，并以水位机制隔离迟到样本；
- **持久化阈值告警**：按设备 + 指标配置上限 / 下限与连续触发、恢复次数，带连续计数的迟滞状态机防抖动，规则状态与告警 / 恢复事件持久化在 SQLite。
- **设备指标校准**：按设备 + 指标维护线性校准配置（`校准值 = 原始值 × scale + offset`），有效时间段互不重叠（事务内校验），按时刻解析唯一生效配置并计算校准值，不改写已存原始测量；窗口聚合支持可选的校准值模式（`calibrated=true`），按每个已提交包的 `sent_at` 解析当时生效的校准区间计算统计值，无配置沿用原始值。
- **只读归档导出**：按设备 + seq 范围创建一次性导出任务，返回 NDJSON 分页游标与 manifest（包数、首尾序号、规范化内容 SHA-256），快照不可变、续传字节稳定，并提供摘要校验接口；导出不触碰提交、缓冲、重复与告警数据。

## 快速开始

### Docker Compose（推荐，单命令）

```bash
docker compose up --build
```

这一条命令完成全部初始化：构建镜像 → 容器内安装依赖（`docker-entrypoint.sh`）→
应用启动钩子自动创建 SQLite 数据库与全部表结构（含告警规则、规则状态、告警事件、校准配置、作废记录表）→ 启动服务。
数据库文件持久化在命名卷 `lab-data`（容器内 `/data/lab.db`），重启不丢规则与事件。

服务地址：http://localhost:8000 ，交互式文档：http://localhost:8000/docs

### 本地开发（单命令初始化 + 起服务）

```bash
pip install -r requirements-dev.txt   # 初始化依赖（含运行时与 pytest）
uvicorn app.main:app --reload         # 数据库默认创建在 ./lab.db，可用 DB_PATH 覆盖
```

另起终端运行测试：

```bash
pytest tests/
```

## 数据模型

仪器上报的测量包：

```json
{
  "device_id": "ph-meter-01",
  "seq": 12,
  "idempotency_key": "ph-meter-01-2026-09-22-0012",
  "sent_at": "2026-09-22T08:00:00Z",
  "measurements": [
    {"metric": "ph", "value": 7.01, "unit": "pH", "ts": "2026-09-22T07:59:58Z"},
    {"metric": "temperature", "value": 21.4, "unit": "C"}
  ]
}
```

- `seq`：包序号，**按设备**从 1 起单调递增、连续；确认丢失的连续序号段可登记**作废**（见「缺包作废」），作废序号被顺序前沿永久跳过、不再期望到达；
- `idempotency_key`：幂等键（也可通过 `Idempotency-Key` 请求头传入，二者至少提供一个）。

## 提交规则

每台设备独立维护 `committed_upto`（已连续提交的最大序号）：

1. **首包自举**：设备第一个到达的包立即提交，`committed_upto` 初始化为 `seq - 1`；
2. 之后到达的包：
   - `seq <= committed_upto` → 重复（`already_committed`）；
   - 已在缓冲区 → 重复（`already_buffered`）；
   - 否则放入缓冲区，并从 `committed_upto + 1` 起连续"排水"提交；
3. 同一幂等键重放 → 返回首次处理结果（`idempotent_replay`）；
4. 同一幂等键携带**不同载荷** → `409 Conflict`；
5. **缺包作废**：确认丢失的缺口段登记作废后，`committed_upto` 越过作废段并立即排水随后连续的缓冲包（见「缺包作废」）；作废序号的迟到补交按 `already_committed` 去重。

所有摄入与作废操作在单事务内完成，保证并发下状态一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/packages` | 上报测量包（幂等、乱序缓冲、按序提交） |
| GET | `/api/v1/devices` | 设备列表及状态汇总 |
| GET | `/api/v1/devices/{id}/gaps` | 缺口：缓冲区序号与缺失序号 |
| GET | `/api/v1/devices/{id}/committed` | 已提交序号（支持 `from_seq`/`to_seq`/`limit`/`offset`） |
| GET | `/api/v1/devices/{id}/duplicates` | 重复包记录（可按 `reason` 过滤） |
| POST | `/api/v1/devices/{id}/voids` | 作废从 `next_expected` 开始的连续缺口段，推进前沿并排水随后缓冲包（幂等） |
| GET | `/api/v1/devices/{id}/voids` | 作废记录查询（`limit`/`offset` 分页） |
| GET | `/api/v1/devices/{id}/metrics/rollup` | 按事件时间的窗口聚合（count/min/max/mean + 水位迟到统计，可选 `calibrated=true` 校准值口径） |
| POST | `/api/v1/rules` | 创建阈值告警规则（按 设备+指标 唯一） |
| GET | `/api/v1/rules` | 规则列表（可按 `device_id`/`metric` 过滤） |
| GET | `/api/v1/rules/{rule_id}` | 规则详情 |
| PUT | `/api/v1/rules/{rule_id}` | 全量更新规则（上下限、连续次数、启用状态） |
| DELETE | `/api/v1/rules/{rule_id}` | 删除规则（历史事件保留） |
| GET | `/api/v1/alerts` | 当前仍处于告警中的规则（可按 `device_id`/`metric` 过滤） |
| GET | `/api/v1/devices/{id}/alerts/events` | 按设备、可选指标分页查询触发/恢复事件 |
| POST | `/api/v1/calibrations` | 创建设备指标线性校准配置（有效时间段不重叠，事务内校验） |
| GET | `/api/v1/devices/{id}/calibrations/resolve` | 按 `metric`/`at`/`value` 解析唯一生效配置并计算校准值（无配置返回 null） |
| POST | `/api/v1/exports` | 按设备 + seq 范围创建只读导出任务（manifest + 首页游标） |
| GET | `/api/v1/exports/{id}/manifest` | 查询导出 manifest |
| GET | `/api/v1/exports/{id}/pages?cursor=` | NDJSON 分页续传（响应头返回下一个游标） |
| POST | `/api/v1/exports/{id}/verify` | 提交导出内容 SHA-256 校验完整性 |

## 缺包作废（Voids）

仪器掉线等原因导致某些序号**永远不会到达**时，缺口会卡住后续所有缓冲包。
作废接口允许运维方登记一段确认丢失的连续序号，让顺序前沿跳过它们。

### 作废段的约束

`POST /api/v1/devices/{device_id}/voids`，请求体：

| 字段 | 必填 | 说明 |
|---|---|---|
| `from_seq` | 是 | 作废段起点（含），**必须等于当前 `next_expected`** |
| `to_seq` | 是 | 作废段终点（含），**不得超过当前缓冲区最大序号** |
| `idempotency_key` | 是 | 幂等键；也可用 `Idempotency-Key` 请求头传入 |

校验规则（不满足即拒绝，不留任何痕迹）：

- 仅允许对**已有设备**发起作废，未知设备 → `404`；
- 作废段必须从 `next_expected` 开始：起点已提交 → `422`（已提交序号不得作废），
  起点跳过更靠前的缺口 → `422`；
- 段内序号当前必须确为缺口：**已缓冲的序号不得作废**（段内含缓冲序号 → `422`）；
- 段尾不得超过当前缓冲区最大序号（缓冲区为空 → `422`）——作废的意义是桥接缺口、
  放行其后滞留的缓冲包；
- `from_seq > to_seq` → `422`。

### 作废的行为

一次作废与摄入**共用串行锁与同一数据库事务**，原子完成：

1. 登记一条作废记录（`GET /api/v1/devices/{id}/voids` 可查）；
2. 顺序前沿 `committed_upto` 推进到段尾；
3. 立即从段尾 + 1 起**排水提交**随后连续的缓冲包，排水与摄入走同一条路径，
   **沿用现有告警评估**（每个真实包恰好评估一次）；
4. 作废序号本身**不伪装成测量包**：不产生 `committed` 行、不参与告警评估、
   不进入导出内容；其迟到补交按 `already_committed` 去重。

### 幂等与冲突

- 相同作废请求重试（同幂等键、同载荷）→ 返回首次处理结果
  （`status="duplicate"`、`duplicate_reason="idempotent_replay"`），
  **不重复推进前沿、不重复登记记录**；
- 同一幂等键携带不同载荷 → `409 Conflict`；
- 校验失败（`422`）的请求不占用幂等键，修正后可用原键重发。

### 接口示例

```bash
# seq 1、2 已提交；4、5 因缺 3 滞留在缓冲区
curl localhost:8000/api/v1/devices/ph-meter-01/gaps
# => {"committed_upto":2,"next_expected":3,"buffered_seqs":[4,5],"gaps":[3]}

# 确认 seq 3 已丢失，登记作废
curl -X POST localhost:8000/api/v1/devices/ph-meter-01/voids \
  -H 'Content-Type: application/json' \
  -d '{"from_seq": 3, "to_seq": 3, "idempotency_key": "void-ph-meter-01-3"}'
# => {"status":"voided","device_id":"ph-meter-01","from_seq":3,"to_seq":3,
#     "committed_upto":5,"committed_seqs":[4,5],"duplicate_reason":null}

# 相同请求重试：不重复推进、不重复登记
curl -X POST localhost:8000/api/v1/devices/ph-meter-01/voids \
  -H 'Content-Type: application/json' \
  -d '{"from_seq": 3, "to_seq": 3, "idempotency_key": "void-ph-meter-01-3"}'
# => {"status":"duplicate","duplicate_reason":"idempotent_replay",...}

# 查询作废记录
curl localhost:8000/api/v1/devices/ph-meter-01/voids
# => {"device_id":"ph-meter-01","count":1,"records":[
#      {"id":1,"device_id":"ph-meter-01","from_seq":3,"to_seq":3,
#       "idempotency_key":"void-ph-meter-01-3","created_at":"..."}]}
```

### 对导出的影响

作废序号永不产生 `committed` 行，因此**导出只包含真实已提交的包**；
当导出范围（缺省或显式）覆盖作废序号时，manifest 的 `complete` 为 `false`，
提示调用方该范围存在永久缺口（见「只读归档导出」）。

## 窗口聚合（Rollup）

`GET /api/v1/devices/{device_id}/metrics/rollup` 按**事件时间**（包的 `sent_at`）聚合**已提交**的测量：

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名，只统计该指标的测量值 |
| `from` / `to` | 是 | 聚合范围 `[from, to)`，ISO 8601（不带时区按 UTC 处理） |
| `window_seconds` | 是 | 窗口宽度（秒），必须为正 |
| `allowed_lateness` | 否 | 允许的迟到时长（秒），默认 `0` |
| `calibrated` | 否 | 是否按校准值统计：`true` / `false`（`1`/`0`、`yes`/`no` 亦可），默认 `false` 统计原始值 |

语义：

- 窗口是以 `from` 为锚点的**半开区间** `[from + k·w, from + (k+1)·w)`，覆盖 `[from, to)`；窗口按 `start` 升序稳定返回，空窗口 `count=0` 且统计值为 `null`；
- **水位** `watermark = 设备已提交包的最大 sent_at - allowed_lateness`（设备级，与查询的指标、时间范围无关）；
- 迟到判定发生在**包提交时刻**：按提交顺序重放，若某包的 `sent_at` 早于当时水位，其样本为迟到样本——计入 `late_count`，**不改写已关闭窗口**的统计；
- 缓冲区中尚未提交的包不参与聚合；无 `sent_at` 的包无法分窗，跳过；
- **校准值模式**（`calibrated=true`）：对每个已提交包按其 **`sent_at`** 解析该（设备，指标）在该时刻唯一生效的校准区间，以 `校准值 = 原始值 × scale + offset` 参与 count/min/max/mean 统计；该时刻无生效配置（区间之前 / 之后 / 从未配置）时**沿用原始值**。响应中回显 `"calibrated": true`。迟到样本在任何模式下都只计入 `late_count`，**不解析校准、不入窗**；校准只在查询时计算，已存测量与归档导出始终保持原始值（见下文校准章节）；
- 单次查询窗口数上限 10000，超出返回 `422`（请增大 `window_seconds` 或缩小范围）。

示例：

```bash
# 默认：统计原始值
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&allowed_lateness=60'
# => {"device_id":"ph-meter-01","metric":"temperature","from":"...","to":"...",
#     "window_seconds":300.0,"allowed_lateness":60.0,"calibrated":false,
#     "watermark":"2026-09-22T08:59:00Z","late_count":0,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.0,"max":21.6,"mean":21.3}, ...]}

# 校准值模式：按每个包 sent_at 当时生效的校准区间计算
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&calibrated=true'
# => {..., "calibrated":true,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.92,"max":22.532,"mean":22.226}, ...]}
# （示例配置 scale=1.02、offset=0.5；无生效配置的包仍按原始值计入同一窗口）
```

## 阈值告警（Threshold Alerts）

按 **设备 + 指标** 配置规则（同一组合仅一条）：

| 字段 | 说明 |
|---|---|
| `upper_limit` / `lower_limit` | 上限 / 下限，**至少提供一个**；严格比较：`value > 上限` 或 `value < 下限` 才算越界，等于阈值不越界 |
| `trigger_count` | 连续越界多少次后触发告警（默认 1） |
| `recovery_count` | 告警后连续多少次不越界才恢复（默认 1），与触发阈值相互独立，形成迟滞 |
| `enabled` | 是否启用；禁用期间不评估，重新启用不回溯历史包 |

### 迟滞状态机（防抖动）

规则状态持久化在 `alert_rule_states`，每条规则一个状态机：

- **normal**：维护连续越界计数 `breach_streak`，越界 +1、不越界清零；达到 `trigger_count` 时翻转为 **triggered** 并写入一条 `triggered` 事件；
- **triggered**：维护连续恢复计数 `recovery_streak`，不越界 +1、再次越界清零（抖动被吸收，不会反复告警）；达到 `recovery_count` 时翻回 **normal** 并写入一条 `recovered` 事件；
- 包内没有该指标的测量值时该规则**跳过、计数不变**（缺数不等于恢复）；一个包内含同一指标多个测量值时，按 measurements 的提交顺序逐个推进状态机。

### 评估时机：只认提交顺序

- 评估只发生在包**按序排水提交**时：缓冲中的包不评估，重复包（幂等重放 / `already_committed` / `already_buffered`）直接短路，**绝不重复产生事件**；
- 乱序缺口被补齐时，缓冲包按 seq 升序一次性排水，每个包**只按这一次提交顺序**评估；
- 规则在建规则之前已提交的历史包**不回溯**；
- 规则配置被更新时，保留当前是否告警的状态，但清零两个连续计数，避免沿用旧阈值的计数误判；
- 删除规则会级联删除其状态（当前告警消失），历史事件保留、`rule_id` 置空。

事件的**时间与序号**：

- 事件序号 `id` 为全表单递增自增，即按实际提交处理顺序编号；`seq` 为触发 / 恢复时处理的包序号（同包内多个测量值可触发+恢复，序号相同、`id` 不同）；
- 事件时间优先取测量值 `ts`，其次取包的 `sent_at`，均无则用提交时刻（UTC，`Z` 结尾）。

### 接口示例

创建规则（温度上限 30：连续 2 次越界触发，连续 2 次回限内恢复）：

```bash
curl -X POST localhost:8000/api/v1/rules -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "upper_limit": 30, "trigger_count": 2, "recovery_count": 2
}'
# => 201 {"id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "upper_limit":30.0,"lower_limit":null,"trigger_count":2,
#        "recovery_count":2,"enabled":true,"created_at":"...","updated_at":"..."}
```

更新（全量替换）/ 删除：

```bash
curl -X PUT localhost:8000/api/v1/rules/1 -H 'Content-Type: application/json' -d '{
  "upper_limit": 40, "lower_limit": 0, "trigger_count": 3,
  "recovery_count": 2, "enabled": true
}'
curl -X DELETE localhost:8000/api/v1/rules/1      # 204；历史事件保留
```

上报测量包（接口与前述完全一致，提交时自动评估）：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 1, "idempotency_key": "k-1",
  "measurements": [{"metric": "temperature", "value": 31.2, "ts": "2026-09-22T08:00:00Z"}]
}'
```

查询当前告警（触发后、恢复前）：

```bash
curl 'localhost:8000/api/v1/alerts?device_id=ph-meter-01'
# => {"count":1,"alerts":[{"rule_id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "upper_limit":30.0,"lower_limit":null,"trigger_count":2,"recovery_count":2,
#        "triggered_seq":4,"triggered_event_id":7,"triggered_at":"2026-09-22T08:00:04Z",
#        "last_value":33.0,"last_seq":4,"recovery_streak":0}]}
```

按设备、指标分页查询事件（`limit`/`offset`，可按 `event_type=triggered|recovered` 过滤）：

```bash
curl 'localhost:8000/api/v1/devices/ph-meter-01/alerts/events?metric=temperature&limit=50&offset=0'
# => {"count":2,"limit":50,"offset":0,"events":[
#       {"id":7,"rule_id":1,"device_id":"ph-meter-01","metric":"temperature","seq":4,
#        "event_type":"triggered","value":33.0,"event_time":"2026-09-22T08:00:04Z","created_at":"..."},
#       {"id":8,"rule_id":1,"device_id":"ph-meter-01","metric":"temperature","seq":8,
#        "event_type":"recovered","value":27.0,"event_time":"2026-09-22T08:00:08Z","created_at":"..."}]}
```

## 设备指标校准（Calibrations）

按 **设备 + 指标** 维护线性校准配置：`校准值 = 原始值 × scale + offset`。
每条配置声明一个半开有效区间 `[valid_from, valid_to)`（`valid_to` 缺省表示无限期），
同一 (设备, 指标) 的有效区间**不得重叠**——创建时在同一事务内完成重叠校验与写入，
重叠返回 `409`，因此任意时刻至多一条配置生效。

校准只在查询时计算，**不改写已存储的原始测量**：已提交数据、归档导出始终保持
原始值；窗口聚合默认（`calibrated=false`）同样统计原始值，仅在显式传
`calibrated=true` 时按校准值统计（见「窗口聚合」一节）。

### 创建配置

`POST /api/v1/calibrations`

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` / `metric` | 是 | 设备号与指标名 |
| `valid_from` | 是 | 生效起点（含），ISO 8601；不带时区按 UTC |
| `valid_to` | 否 | 生效终点（不含），ISO 8601；缺省表示无限期 |
| `offset` | 否 | 线性偏移，默认 `0` |
| `scale` | 否 | 线性增益，默认 `1` |

```bash
curl -X POST localhost:8000/api/v1/calibrations -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z",
  "offset": 0.5, "scale": 1.02
}'
# => 201 {"id":1,"device_id":"ph-meter-01","metric":"temperature",
#        "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z",
#        "offset":0.5,"scale":1.02,"created_at":"..."}
```

错误：`valid_from >= valid_to` → `422`；与同 (设备, 指标) 已有配置的有效区间重叠
→ `409`（相接的半开区间，如一段终点等于另一段起点，不算重叠）。

### 按时刻解析并计算校准值

`GET /api/v1/devices/{device_id}/calibrations/resolve?metric=&at=&value=`

| 参数 | 必填 | 说明 |
|---|---|---|
| `metric` | 是 | 指标名 |
| `at` | 是 | 生效时刻，ISO 8601（不带时区按 UTC）；区间边界为 `[valid_from, valid_to)` |
| `value` | 是 | 原始测量值 |

```bash
curl 'localhost:8000/api/v1/devices/ph-meter-01/calibrations/resolve?metric=temperature&at=2026-09-15T08:00:00Z&value=21.4'
# => {"device_id":"ph-meter-01","metric":"temperature","at":"2026-09-15T08:00:00Z",
#     "value":21.4,"calibrated_value":22.328,
#     "calibration":{"id":1,"device_id":"ph-meter-01","metric":"temperature",
#                    "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z",
#                    "offset":0.5,"scale":1.02,"created_at":"..."}}
```

无生效配置时 HTTP 仍为 `200`，`calibration` 与 `calibrated_value` 均为 `null`。

### 窗口聚合的校准值模式（calibrated=true）

窗口聚合接口 `GET /api/v1/devices/{device_id}/metrics/rollup` 额外支持布尔查询参数
`calibrated`（默认 `false`）：

- `false` / 缺省：count / min / max / mean 全部基于**原始值**，与校准配置是否存在无关；
- `true`：服务把该（设备，指标）的全部校准区间一次性载入，对**每个已提交包**按其
  `sent_at` 二分解析当时唯一生效的区间（边界同样是 `[valid_from, valid_to)`），
  包内该指标的每个测量值统一按 `原始值 × scale + offset` 转换后再入窗；该包
  `sent_at` 时刻无生效配置时，包内样本**沿用原始值**——因此同一窗口中可能既有
  校准值（命中配置的包）也有原始值（未命中的包）。

其余语义保持不变：

- 迟到样本仍只计入 `late_count`，不解析校准、不进入任何窗口，已关闭窗口的统计不被改写；
- 校准只发生在本次查询期间，可任意重复调用；**已存测量与归档导出（NDJSON / manifest
  摘要）始终是原始值**，水位、窗口划分与 `late_count` 也与原始值模式完全一致；
- 响应体回显 `"calibrated": true|false`，便于下游区分两种口径。

```bash
# 先配置一段生效区间
curl -X POST localhost:8000/api/v1/calibrations -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "metric": "temperature",
  "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z",
  "offset": 0.5, "scale": 1.02
}'

# 原始值口径（默认）
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300'
# 校准值口径：21.4 -> 21.4*1.02+0.5 = 22.328
curl 'localhost:8000/api/v1/devices/ph-meter-01/metrics/rollup?metric=temperature&from=2026-09-22T08:00:00Z&to=2026-09-22T09:00:00Z&window_seconds=300&calibrated=true'
# => {"device_id":"ph-meter-01", ..., "calibrated":true,
#     "windows":[{"start":"...","end":"...","count":3,"min":21.92,"max":22.532,"mean":22.226}, ...]}
```

## 只读归档导出（Exports）

对某台设备按 **seq 范围**创建一次只读导出任务。任务在创建时刻把范围内
**当时已提交**的包复制为不可变快照（独立的 `export_tasks` / `export_items` 表），
之后设备继续摄入、排水提交都不会改变已有导出的内容与摘要；缓冲包、重复包、
告警规则与事件**全程不参与、也不被修改**。

### 创建任务

`POST /api/v1/exports`

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` | 是 | 设备号 |
| `from_seq` / `to_seq` | 否 | 闭区间范围；缺省分别表示从首包开始 / 到创建时刻已提交末包为止 |
| `page_size` | 否 | 每页 NDJSON 行数，默认 100，范围 1..1000（游标中固化，续传时不可变） |

```bash
curl -X POST localhost:8000/api/v1/exports -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "from_seq": 1, "to_seq": 100, "page_size": 50
}'
# => 201
# {"export_id":"a1b2c3...","device_id":"ph-meter-01","from_seq":1,"to_seq":100,
#  "page_size":50,"package_count":100,"first_seq":1,"last_seq":100,
#  "complete":true,"content_sha256":"9f86d0...","created_at":"...",
#  "next_cursor":"eyJpZCI6..."}
```

Manifest 字段：

- `package_count`：快照包数；`first_seq` / `last_seq`：快照首尾序号（空导出为 `null`）；
- `complete`：请求范围是否被创建时刻的已提交数据**连续完整覆盖**——
  范围越出已提交边界、范围内仍有缺口（缓冲未排水）、或范围内含**已作废序号**
  （作废段永不产生已提交包）时为 `false`，提示调用方快照只是部分内容；
- `content_sha256`：全量导出内容（按 seq 升序逐行 NDJSON、UTF-8）的 SHA-256。

### NDJSON 分页与游标续传

`GET /api/v1/exports/{export_id}/pages?cursor=...`（无 cursor 时从首页开始）

- 响应体为 **NDJSON**（`application/x-ndjson`）：每个已提交包一行，
  行内容是规范化（键排序）后的测量包 JSON；整段内容与 manifest 的 `content_sha256` 对应；
- 响应头：
  - `X-Next-Cursor`：下一页游标；**末页不返回**；
  - `X-Export-Done`：`true` / `false`，本页是否最后一页；
  - `X-Page-Offset`：本页起始行偏移；`X-Package-Count`：快照总包数；
  - `X-Content-SHA256`：与 manifest 相同的内容摘要。
- **稳定性**：同一游标可任意重复请求，返回字节完全一致；逐页拼接后计算
  `sha256` 必然等于 manifest 摘要，可边下边校验。
- 错误：任务不存在 → `404`；游标损坏、属于其他任务、页大小不匹配或偏移越界
  （例如末页之后继续翻页）→ `400`，`detail` 给出具体原因。

```bash
# 首页
curl -D headers.txt localhost:8000/api/v1/exports/$EID/pages -o page0.ndjson
NEXT=$(grep -i '^X-Next-Cursor:' headers.txt | cut -d' ' -f2 | tr -d '\r')
# 续传
curl localhost:8000/api/v1/exports/$EID/pages?cursor="$NEXT" -o page1.ndjson
# 校验（逐页拼接等价于对全量内容摘要）
cat page0.ndjson page1.ndjson | sha256sum
```

### Manifest 查询与完整性校验

```bash
curl localhost:8000/api/v1/exports/$EID/manifest

curl -X POST localhost:8000/api/v1/exports/$EID/verify \
  -H 'Content-Type: application/json' \
  -d '{"content_sha256":"9f86d0..."}'
# => 200（注意：摘要不匹配时业务结果 valid=false，HTTP 状态仍为 200；任务不存在才是 404）
# {"valid":true,"expected_sha256":"9f86d0...","received_sha256":"9f86d0...",
#  "manifest":{ ... }}
```

### 示例

上报乱序包（seq 2 先到，进入缓冲）：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 2, "idempotency_key": "k-2",
  "measurements": [{"metric": "ph", "value": 7.01}]
}'
# => {"status":"buffered","committed_upto":0,"committed_seqs":[],...}
```

查询缺口：

```bash
curl localhost:8000/api/v1/devices/ph-meter-01/gaps
# => {"committed_upto":0,"next_expected":1,"buffered_seqs":[2],"gaps":[1]}
```

补齐缺口后按序提交：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '{
  "device_id": "ph-meter-01", "seq": 1, "idempotency_key": "k-1",
  "measurements": [{"metric": "ph", "value": 6.98}]
}'
# => {"status":"committed","committed_seqs":[1,2],"committed_upto":2,...}
```

重复重放与查询：

```bash
curl -X POST localhost:8000/api/v1/packages -H 'Content-Type: application/json' -d '<同上请求体>'
# => {"status":"duplicate","duplicate_reason":"idempotent_replay",...}

curl localhost:8000/api/v1/devices/ph-meter-01/committed    # 已提交序号
curl localhost:8000/api/v1/devices/ph-meter-01/duplicates   # 重复包记录
```

## 项目结构

```
app/
  main.py         # FastAPI 路由
  logic.py        # 摄入核心：幂等判定、缓冲、排水提交（提交时驱动告警评估）
  voids.py        # 缺包作废：作废段校验、登记、前沿推进与排水（与摄入共用锁、事务与排水路径）
  alerts.py       # 阈值告警：规则 CRUD、带连续计数的迟滞状态机、按提交顺序评估
  calibrations.py # 设备指标校准：有效区间不重叠校验、按时刻解析与线性校准计算（含区间索引供聚合批量解析）
  rollup.py       # 窗口聚合：半开窗口分桶、水位迟到判定、可选校准值口径
  exports.py      # 只读归档导出：不可变快照、NDJSON 游标编解码、manifest 摘要校验
  db.py           # SQLite 连接与建表（启动时自动执行）
  schemas.py      # Pydantic 模型
tests/
  conftest.py           # 测试数据库隔离（临时文件）
  test_api.py           # 摄入 / 查询接口测试
  test_voids.py         # 缺包作废：校验 / 排水 / 幂等 / 告警 / 导出测试
  test_rollup.py        # 窗口聚合测试
  test_alerts.py        # 阈值告警测试
  test_calibrations.py  # 校准配置 / 解析 / 原始数据不变测试
  test_exports.py       # 归档导出 / 游标 / 摘要校验测试
Dockerfile / docker-entrypoint.sh / docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | 容器内 `/data/lab.db`，本地 `./lab.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（entrypoint 使用） |
