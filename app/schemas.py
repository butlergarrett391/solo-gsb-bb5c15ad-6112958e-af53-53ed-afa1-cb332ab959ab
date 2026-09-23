"""请求 / 响应的数据模型。"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Measurement(BaseModel):
    metric: str = Field(min_length=1, description="指标名，如 temperature")
    value: float
    unit: Optional[str] = None
    ts: Optional[datetime] = None


class PackageIn(BaseModel):
    """仪器上报的一个批量测量包。"""

    device_id: str = Field(min_length=1)
    seq: int = Field(ge=1, description="包序号，按设备单调递增、连续")
    idempotency_key: Optional[str] = Field(
        default=None, description="幂等键；也可用 Idempotency-Key 请求头传入"
    )
    sent_at: Optional[datetime] = None
    measurements: List[Measurement] = Field(min_length=1)


class IngestResponse(BaseModel):
    status: Literal["committed", "buffered", "duplicate"]
    device_id: str
    seq: int
    committed_upto: int
    committed_seqs: List[int] = Field(
        default_factory=list, description="本次请求触发提交的全部序号（含排水）"
    )
    duplicate_reason: Optional[str] = None


class DeviceState(BaseModel):
    device_id: str
    committed_upto: int = Field(
        description="已连续处理（提交或作废）的最大序号"
    )
    next_expected: int
    buffered_count: int
    committed_count: int
    duplicate_count: int
    voided_count: int = Field(description="已作废序号总数（缺包作废段长度之和）")


class GapsResponse(BaseModel):
    device_id: str
    committed_upto: int
    next_expected: int
    buffered_seqs: List[int]
    gaps: List[int] = Field(description="缓冲区最大序号之前缺失的序号")


class CommittedResponse(BaseModel):
    device_id: str
    committed_upto: int
    count: int
    seqs: List[int]


class DuplicateRecord(BaseModel):
    id: int
    device_id: str
    seq: Optional[int]
    idempotency_key: Optional[str]
    reason: str
    received_at: str


class DuplicatesResponse(BaseModel):
    device_id: str
    count: int
    records: List[DuplicateRecord]


# ---------------------------------------------------------------------------
# 缺包作废
# ---------------------------------------------------------------------------

class VoidCreate(BaseModel):
    """对一段确定缺失的连续序号发起作废。"""

    from_seq: int = Field(
        ge=1, description="作废段起点（含）；必须恰好等于当前 next_expected"
    )
    to_seq: int = Field(
        ge=1, description="作废段终点（含）；不得超过当前缓冲区最大序号"
    )
    idempotency_key: Optional[str] = Field(
        default=None, description="幂等键；也可用 Idempotency-Key 请求头传入"
    )

    @model_validator(mode="after")
    def _check_range(self):
        if self.from_seq > self.to_seq:
            raise ValueError("from_seq 不能大于 to_seq")
        return self


class VoidResponse(BaseModel):
    status: Literal["voided"]
    device_id: str
    from_seq: int = Field(description="作废段起点（含）")
    to_seq: int = Field(description="作废段终点（含）")
    voided_seqs: List[int] = Field(description="本次登记作废的全部序号")
    committed_upto: int = Field(
        description="作废并排水后已连续处理（提交或作废）的最大序号"
    )
    committed_seqs: List[int] = Field(
        default_factory=list,
        description="作废后立即排水提交的真实缓冲包序号（不含作废序号）",
    )


class VoidRecordOut(BaseModel):
    """一条作废登记：一段连续的作废序号。"""

    id: int
    device_id: str
    from_seq: int = Field(description="作废段起点（含）")
    to_seq: int = Field(description="作废段终点（含）")
    voided_seqs: List[int] = Field(description="该段作废的全部序号")
    created_at: str


class VoidsResponse(BaseModel):
    device_id: str
    committed_upto: int = Field(
        description="已连续处理（提交或作废）的最大序号"
    )
    next_expected: int
    count: int = Field(description="作废登记段数")
    voided_count: int = Field(description="已作废序号总数")
    records: List[VoidRecordOut]


class RollupWindow(BaseModel):
    """单个半开窗口 [start, end) 的聚合结果；空窗口 count=0 且统计值为 null。"""

    start: datetime
    end: datetime
    count: int
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None


class RollupResponse(BaseModel):
    """按事件时间（sent_at）的窗口聚合结果。"""

    model_config = {"populate_by_name": True}

    device_id: str
    metric: str
    from_: datetime = Field(alias="from", description="聚合范围起点（含）")
    to: datetime = Field(description="聚合范围终点（不含）")
    window_seconds: float
    allowed_lateness: float
    calibrated: bool = Field(
        default=False,
        description="统计值是否为校准值：true 时按包 sent_at 生效的校准区间计算，"
        "无生效配置沿用原始值；false 时始终为原始值",
    )
    watermark: Optional[datetime] = Field(
        default=None,
        description="设备已提交数据的最大 sent_at 减 allowed_lateness；无数据时为 null",
    )
    late_count: int = Field(
        description="范围内 sent_at 早于水位的迟到样本数（不改写已关闭窗口）"
    )
    windows: List[RollupWindow] = Field(description="按 start 升序的全部窗口")


# ---------------------------------------------------------------------------
# 阈值告警
# ---------------------------------------------------------------------------

class RuleBase(BaseModel):
    """规则的可配置字段（创建与全量更新共用）。"""

    upper_limit: Optional[float] = Field(
        default=None, description="上限；value > upper_limit 视为越界，null 表示不限"
    )
    lower_limit: Optional[float] = Field(
        default=None, description="下限；value < lower_limit 视为越界，null 表示不限"
    )
    trigger_count: int = Field(
        default=1, ge=1, description="连续越界多少次后触发告警"
    )
    recovery_count: int = Field(
        default=1, ge=1, description="告警后连续不越界多少次后恢复"
    )
    enabled: bool = Field(default=True, description="是否参与评估")

    @model_validator(mode="after")
    def _check_limits(self):
        if self.upper_limit is None and self.lower_limit is None:
            raise ValueError("upper_limit 与 lower_limit 至少需要提供一个")
        if (
            self.upper_limit is not None
            and self.lower_limit is not None
            and self.lower_limit > self.upper_limit
        ):
            raise ValueError("lower_limit 不能大于 upper_limit")
        return self


class RuleCreate(RuleBase):
    device_id: str = Field(min_length=1)
    metric: str = Field(min_length=1, description="指标名，如 temperature")


class RuleUpdate(RuleBase):
    """PUT 全量更新；device_id 与 metric 不可修改。"""


class RuleOut(BaseModel):
    id: int
    device_id: str
    metric: str
    upper_limit: Optional[float]
    lower_limit: Optional[float]
    trigger_count: int
    recovery_count: int
    enabled: bool
    created_at: str
    updated_at: str


class RuleListResponse(BaseModel):
    count: int
    rules: List[RuleOut]


class AlertEventOut(BaseModel):
    """一次触发或恢复事件；id 为按提交顺序单调递增的事件序号。"""

    id: int
    rule_id: Optional[int] = Field(
        description="规则删除后为 null，事件本身仍可查询"
    )
    device_id: str
    metric: str
    seq: int = Field(description="触发 / 恢复时处理的包序号")
    event_type: Literal["triggered", "recovered"]
    value: float = Field(description="触发或恢复的那个测量值")
    event_time: Optional[str] = Field(
        description="触发 / 恢复时间：优先取测量 ts，其次包 sent_at，再无则提交时刻"
    )
    created_at: str = Field(description="事件落库时间")


class AlertEventsResponse(BaseModel):
    count: int
    limit: int
    offset: int
    events: List[AlertEventOut]


class CurrentAlertOut(BaseModel):
    """当前仍处于告警中的规则状态（规则状态为 triggered 且规则启用）。"""

    rule_id: int
    device_id: str
    metric: str
    upper_limit: Optional[float]
    lower_limit: Optional[float]
    trigger_count: int
    recovery_count: int
    triggered_seq: int = Field(description="触发告警的包序号")
    triggered_event_id: int = Field(description="触发事件的事件序号")
    triggered_at: Optional[str] = Field(description="触发时间")
    last_value: Optional[float]
    last_seq: Optional[int]
    recovery_streak: int = Field(description="当前连续不越界计数（未达恢复次数）")


class CurrentAlertsResponse(BaseModel):
    count: int
    alerts: List[CurrentAlertOut]


# ---------------------------------------------------------------------------
# 设备指标校准
# ---------------------------------------------------------------------------

class CalibrationCreate(BaseModel):
    """创建一条线性校准配置：校准值 = 原始值 * scale + offset。"""

    device_id: str = Field(min_length=1)
    metric: str = Field(min_length=1, description="指标名，如 temperature")
    valid_from: datetime = Field(description="生效起点（含），ISO 8601；不带时区按 UTC")
    valid_to: Optional[datetime] = Field(
        default=None, description="生效终点（不含），ISO 8601；缺省表示无限期"
    )
    offset: float = Field(default=0.0, description="线性偏移")
    scale: float = Field(default=1.0, description="线性增益")

    @model_validator(mode="after")
    def _check_range(self):
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("valid_from 必须早于 valid_to")
        return self


class CalibrationOut(BaseModel):
    id: int
    device_id: str
    metric: str
    valid_from: datetime
    valid_to: Optional[datetime]
    offset: float
    scale: float
    created_at: str


class CalibrationResolveResponse(BaseModel):
    """按时刻解析生效校准配置并计算校准值；无生效配置时 calibration 为 null。"""

    device_id: str
    metric: str
    at: datetime
    value: float = Field(description="原始测量值")
    calibrated_value: Optional[float] = Field(
        description="校准值 = value * scale + offset；无生效配置时为 null"
    )
    calibration: Optional[CalibrationOut] = Field(
        description="at 时刻唯一生效的校准配置；无配置为 null"
    )


# ---------------------------------------------------------------------------
# 只读归档导出
# ---------------------------------------------------------------------------

class ExportCreate(BaseModel):
    """创建一次按设备 + seq 范围的只读导出任务。"""

    device_id: str = Field(min_length=1)
    from_seq: Optional[int] = Field(
        default=None, ge=1, description="起始序号（含）；缺省从首包开始"
    )
    to_seq: Optional[int] = Field(
        default=None, ge=1, description="结束序号（含）；缺省到创建时刻已提交末包"
    )
    page_size: int = Field(
        default=100, ge=1, le=1000, description="每页 NDJSON 行数"
    )

    @model_validator(mode="after")
    def _check_range(self):
        if (
            self.from_seq is not None
            and self.to_seq is not None
            and self.from_seq > self.to_seq
        ):
            raise ValueError("from_seq 不能大于 to_seq")
        return self


class ExportManifest(BaseModel):
    """导出任务清单：范围、包数、首尾序号与规范化内容摘要。"""

    export_id: str
    device_id: str
    from_seq: Optional[int]
    to_seq: Optional[int]
    page_size: int
    package_count: int = Field(description="快照中的包数")
    first_seq: Optional[int] = Field(description="快照首序号；空导出为 null")
    last_seq: Optional[int] = Field(description="快照末序号；空导出为 null")
    complete: bool = Field(
        description="请求的 seq 范围是否被创建时刻已提交数据连续完整覆盖"
    )
    content_sha256: str = Field(
        description="全量 NDJSON（按 seq 升序，逐行 sha256）的 SHA-256"
    )
    created_at: str


class ExportCreateResponse(ExportManifest):
    next_cursor: str = Field(description="首页游标（offset=0）")


class ExportVerifyRequest(BaseModel):
    content_sha256: str = Field(
        min_length=1, description="客户端对导出 NDJSON 内容计算的 SHA-256（十六进制）"
    )


class ExportVerifyResponse(BaseModel):
    valid: bool
    expected_sha256: str
    received_sha256: str
    manifest: ExportManifest
