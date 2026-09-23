"""FastAPI 路由与应用入口。"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response

from . import db
from .alerts import (
    AlertRuleConflict,
    AlertRuleError,
    create_rule,
    delete_rule,
    get_rule,
    update_rule,
)
from .calibrations import (
    CalibrationConflict,
    CalibrationError,
    apply_calibration,
    create_calibration,
    resolve_calibration,
)
from . import exports
from .exports import ExportError, InvalidCursor
from .logic import IdempotencyConflict, ingest_package
from .rollup import RollupError, compute_rollup
from .voids import VoidError, void_range
from .schemas import (
    AlertEventsResponse,
    AlertEventOut,
    CurrentAlertOut,
    CurrentAlertsResponse,
    CalibrationCreate,
    CalibrationOut,
    CalibrationResolveResponse,
    CommittedResponse,
    DeviceState,
    DuplicatesResponse,
    DuplicateRecord,
    ExportCreate,
    ExportCreateResponse,
    ExportManifest,
    ExportVerifyRequest,
    ExportVerifyResponse,
    GapsResponse,
    IngestResponse,
    PackageIn,
    RollupResponse,
    RuleCreate,
    RuleListResponse,
    RuleOut,
    RuleUpdate,
    VoidCreate,
    VoidRecord,
    VoidResponse,
    VoidsResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 容器/进程启动时自动建库建表
    yield


app = FastAPI(
    title="Lab Instrument Ingest API",
    version="1.1.0",
    description="实验室仪器批量测量包接收服务：幂等去重、乱序缓冲、按序提交、缺包作废。",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/packages", response_model=IngestResponse, status_code=200)
def post_package(
    pkg: PackageIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    key = pkg.idempotency_key or idempotency_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail="缺少幂等键：请在请求体 idempotency_key 或 Idempotency-Key 请求头中提供",
        )
    with db.ingest_lock, db.get_conn() as conn:
        try:
            return ingest_package(conn, pkg, key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/devices", response_model=List[DeviceState])
def list_devices():
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id,
                   d.committed_upto,
                   (SELECT COUNT(*) FROM buffer    b WHERE b.device_id = d.device_id) AS buffered_count,
                   (SELECT COUNT(*) FROM committed c WHERE c.device_id = d.device_id) AS committed_count,
                   (SELECT COUNT(*) FROM duplicates p WHERE p.device_id = d.device_id) AS duplicate_count
            FROM devices d ORDER BY d.device_id
            """
        ).fetchall()
    return [
        DeviceState(
            device_id=r["device_id"],
            committed_upto=r["committed_upto"],
            next_expected=r["committed_upto"] + 1,
            buffered_count=r["buffered_count"],
            committed_count=r["committed_count"],
            duplicate_count=r["duplicate_count"],
        )
        for r in rows
    ]


@app.get("/api/v1/devices/{device_id}/gaps", response_model=GapsResponse)
def get_gaps(device_id: str):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        committed_upto = dev["committed_upto"]
        buffered = [
            r["seq"]
            for r in conn.execute(
                "SELECT seq FROM buffer WHERE device_id = ? ORDER BY seq", (device_id,)
            ).fetchall()
        ]
    gaps: List[int] = []
    if buffered:
        have = set(buffered)
        gaps = [s for s in range(committed_upto + 1, max(buffered) + 1) if s not in have]
    return GapsResponse(
        device_id=device_id,
        committed_upto=committed_upto,
        next_expected=committed_upto + 1,
        buffered_seqs=buffered,
        gaps=gaps,
    )


@app.get("/api/v1/devices/{device_id}/committed", response_model=CommittedResponse)
def get_committed(
    device_id: str,
    from_seq: Optional[int] = Query(default=None, ge=1),
    to_seq: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        where, params = "device_id = ?", [device_id]
        if from_seq is not None:
            where += " AND seq >= ?"
            params.append(from_seq)
        if to_seq is not None:
            where += " AND seq <= ?"
            params.append(to_seq)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM committed WHERE {where}", params
        ).fetchone()["n"]
        seqs = [
            r["seq"]
            for r in conn.execute(
                f"SELECT seq FROM committed WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
    return CommittedResponse(
        device_id=device_id,
        committed_upto=dev["committed_upto"],
        count=total,
        seqs=seqs,
    )


@app.get("/api/v1/devices/{device_id}/duplicates", response_model=DuplicatesResponse)
def get_duplicates(
    device_id: str,
    reason: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        where, params = "device_id = ?", [device_id]
        if reason:
            where += " AND reason = ?"
            params.append(reason)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM duplicates WHERE {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM duplicates WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return DuplicatesResponse(
        device_id=device_id,
        count=total,
        records=[DuplicateRecord(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# 缺包作废
# ---------------------------------------------------------------------------

@app.post("/api/v1/devices/{device_id}/voids", response_model=VoidResponse, status_code=200)
def post_void(
    device_id: str,
    void_in: VoidCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """作废从 next_expected 开始的连续缺口段，推进顺序前沿并排水随后缓冲包。

    与摄入共用串行锁与事务；携带幂等键可安全重试（重放返回首次结果）。
    """
    key = void_in.idempotency_key or idempotency_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail="缺少幂等键：请在请求体 idempotency_key 或 Idempotency-Key 请求头中提供",
        )
    with db.ingest_lock, db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return void_range(
                conn, device_id, void_in.from_seq, void_in.to_seq, key
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VoidError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/devices/{device_id}/voids", response_model=VoidsResponse)
def get_voids(
    device_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """按设备分页查询作废记录，按登记顺序（id）升序返回。"""
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM voids WHERE device_id = ?", (device_id,)
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM voids WHERE device_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (device_id, limit, offset),
        ).fetchall()
    return VoidsResponse(
        device_id=device_id,
        count=total,
        records=[VoidRecord(**dict(r)) for r in rows],
    )


@app.get("/api/v1/devices/{device_id}/metrics/rollup", response_model=RollupResponse)
def get_metrics_rollup(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    from_: datetime = Query(alias="from", description="聚合范围起点（含），ISO 8601"),
    to: datetime = Query(description="聚合范围终点（不含），ISO 8601"),
    window_seconds: float = Query(gt=0, description="窗口宽度（秒）"),
    allowed_lateness: float = Query(
        default=0.0, ge=0, description="允许的迟到时长（秒），用于计算水位"
    ),
    calibrated: bool = Query(
        default=False,
        description="true 时按每个已提交包 sent_at 生效的校准区间计算统计值，"
        "无生效配置沿用原始值；false（默认）统计原始值",
    ),
):
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        try:
            return compute_rollup(
                conn,
                device_id,
                metric,
                from_,
                to,
                window_seconds,
                allowed_lateness,
                calibrated,
            )
        except RollupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 阈值告警规则
# ---------------------------------------------------------------------------

@app.post("/api/v1/rules", response_model=RuleOut, status_code=201)
def post_rule(rule_in: RuleCreate):
    with db.ingest_lock, db.get_conn() as conn:
        try:
            rule = create_rule(
                conn,
                rule_in.device_id,
                rule_in.metric,
                rule_in.upper_limit,
                rule_in.lower_limit,
                rule_in.trigger_count,
                rule_in.recovery_count,
                rule_in.enabled,
            )
        except AlertRuleConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlertRuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RuleOut(**conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule.id,)
        ).fetchone())


@app.get("/api/v1/rules", response_model=RuleListResponse)
def list_rules(
    device_id: Optional[str] = Query(default=None, description="按设备过滤"),
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
):
    where, params = [], []
    if device_id:
        where.append("device_id = ?")
        params.append(device_id)
    if metric:
        where.append("metric = ?")
        params.append(metric)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM alert_rules {clause} ORDER BY id", params
        ).fetchall()
    return RuleListResponse(count=len(rows), rules=[RuleOut(**dict(r)) for r in rows])


def _get_rule_or_404(conn, rule_id: int):
    rule = get_rule(conn, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return rule


@app.get("/api/v1/rules/{rule_id}", response_model=RuleOut)
def get_rule_detail(rule_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
        return RuleOut(**dict(row))


@app.put("/api/v1/rules/{rule_id}", response_model=RuleOut)
def put_rule(rule_id: int, rule_in: RuleUpdate):
    with db.ingest_lock, db.get_conn() as conn:
        _get_rule_or_404(conn, rule_id)
        try:
            update_rule(
                conn,
                rule_id,
                rule_in.upper_limit,
                rule_in.lower_limit,
                rule_in.trigger_count,
                rule_in.recovery_count,
                rule_in.enabled,
            )
        except AlertRuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return RuleOut(**dict(row))


@app.delete("/api/v1/rules/{rule_id}", status_code=204)
def remove_rule(rule_id: int):
    with db.ingest_lock, db.get_conn() as conn:
        if not delete_rule(conn, rule_id):
            raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return None


# ---------------------------------------------------------------------------
# 当前告警与告警事件
# ---------------------------------------------------------------------------

@app.get("/api/v1/alerts", response_model=CurrentAlertsResponse)
def get_current_alerts(
    device_id: Optional[str] = Query(default=None, description="按设备过滤"),
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
):
    """当前仍处于告警状态的规则。triggered_* 取自最近一次触发事件。"""
    where, params = ["s.state = 'triggered'", "r.enabled = 1"], []
    if device_id:
        where.append("r.device_id = ?")
        params.append(device_id)
    if metric:
        where.append("r.metric = ?")
        params.append(metric)
    clause = " AND ".join(where)
    sql = f"""
        SELECT r.id               AS rule_id,
               r.device_id        AS device_id,
               r.metric           AS metric,
               r.upper_limit      AS upper_limit,
               r.lower_limit      AS lower_limit,
               r.trigger_count    AS trigger_count,
               r.recovery_count   AS recovery_count,
               t.seq              AS triggered_seq,
               t.id               AS triggered_event_id,
               t.event_time       AS triggered_at,
               s.last_value       AS last_value,
               s.last_seq         AS last_seq,
               s.recovery_streak  AS recovery_streak
          FROM alert_rule_states s
          JOIN alert_rules r ON r.id = s.rule_id
          JOIN alert_events t
            ON t.id = (
                SELECT MAX(e.id) FROM alert_events e
                 WHERE e.rule_id = r.id AND e.event_type = 'triggered'
            )
         WHERE {clause}
         ORDER BY t.id
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return CurrentAlertsResponse(
        count=len(rows), alerts=[CurrentAlertOut(**dict(r)) for r in rows]
    )


@app.get("/api/v1/devices/{device_id}/alerts/events", response_model=AlertEventsResponse)
def get_device_alert_events(
    device_id: str,
    metric: Optional[str] = Query(default=None, description="按指标过滤"),
    event_type: Optional[str] = Query(
        default=None, description="按事件类型过滤：triggered / recovered"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """按设备（可选指标）分页查询告警事件，按事件序号升序返回。"""
    with db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail=f"未知设备: {device_id}")
        if event_type not in (None, "triggered", "recovered"):
            raise HTTPException(
                status_code=422,
                detail="event_type 仅支持 triggered 或 recovered",
            )
        where, params = ["device_id = ?"], [device_id]
        if metric:
            where.append("metric = ?")
            params.append(metric)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        clause = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM alert_events WHERE {clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM alert_events WHERE {clause} ORDER BY id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return AlertEventsResponse(
        count=total,
        limit=limit,
        offset=offset,
        events=[AlertEventOut(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# 设备指标校准
# ---------------------------------------------------------------------------

@app.post("/api/v1/calibrations", response_model=CalibrationOut, status_code=201)
def post_calibration(cal_in: CalibrationCreate):
    """创建线性校准配置；同 (设备, 指标) 有效时间段重叠时返回 409。"""
    with db.ingest_lock, db.get_conn() as conn:
        try:
            cal_id = create_calibration(
                conn,
                cal_in.device_id,
                cal_in.metric,
                cal_in.valid_from,
                cal_in.valid_to,
                cal_in.offset,
                cal_in.scale,
            )
        except CalibrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CalibrationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = conn.execute(
            "SELECT * FROM calibrations WHERE id = ?", (cal_id,)
        ).fetchone()
        return CalibrationOut(**dict(row))


@app.get(
    "/api/v1/devices/{device_id}/calibrations/resolve",
    response_model=CalibrationResolveResponse,
)
def resolve_device_calibration(
    device_id: str,
    metric: str = Query(min_length=1, description="指标名，如 temperature"),
    at: datetime = Query(description="生效时刻，ISO 8601；不带时区按 UTC"),
    value: float = Query(description="原始测量值"),
):
    """按 at 解析 (设备, 指标) 唯一生效的校准配置并计算校准值。

    校准值 = value * scale + offset；无生效配置时 calibration 与
    calibrated_value 均为 null。只读操作，不改写已存储的原始测量。
    """
    with db.get_conn() as conn:
        row = resolve_calibration(conn, device_id, metric, at)
    calibration = CalibrationOut(**dict(row)) if row is not None else None
    calibrated = (
        apply_calibration(value, row["scale"], row["offset"])
        if row is not None
        else None
    )
    return CalibrationResolveResponse(
        device_id=device_id,
        metric=metric,
        at=at,
        value=value,
        calibrated_value=calibrated,
        calibration=calibration,
    )


# ---------------------------------------------------------------------------
# 只读归档导出（NDJSON 分页 + manifest 校验）
# ---------------------------------------------------------------------------

def _get_export_or_404(conn, export_id: str):
    task = exports.get_task(conn, export_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"导出任务不存在: {export_id}")
    return task


@app.post("/api/v1/exports", response_model=ExportCreateResponse, status_code=201)
def post_export(export_in: ExportCreate):
    """按设备 + seq 范围创建只读快照导出，返回 manifest 与首页游标。

    仅读取创建时刻已提交的包；缓冲、重复、告警数据均不受影响。
    """
    with db.ingest_lock, db.get_conn() as conn:
        dev = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (export_in.device_id,)
        ).fetchone()
        if dev is None:
            raise HTTPException(
                status_code=404, detail=f"未知设备: {export_in.device_id}"
            )
        try:
            task = exports.create_export(
                conn,
                export_in.device_id,
                export_in.from_seq,
                export_in.to_seq,
                export_in.page_size,
            )
        except ExportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = exports.manifest_dict(task)
    body["next_cursor"] = exports.first_cursor(task)
    return ExportCreateResponse(**body)


@app.get("/api/v1/exports/{export_id}/manifest", response_model=ExportManifest)
def get_export_manifest(export_id: str):
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        return ExportManifest(**exports.manifest_dict(task))


@app.get("/api/v1/exports/{export_id}/pages")
def get_export_page(
    export_id: str,
    cursor: Optional[str] = Query(
        default=None, description="续传游标；缺省从首页（offset=0）开始"
    ),
):
    """按游标取下一页 NDJSON（每包一行），稳定内容与摘要，可任意重复读取。

    响应头 X-Next-Cursor 给出下一页游标，末页不返回该头并带 X-Export-Done: true。
    """
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        cursor = cursor or exports.first_cursor(task)
        try:
            page = exports.fetch_page(conn, task, cursor)
        except InvalidCursor as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers = {
        "X-Export-Id": task.export_id,
        "X-Page-Offset": str(page["offset"]),
        "X-Package-Count": str(task.package_count),
        "X-Content-SHA256": task.content_sha256,
        "X-Export-Done": "true" if page["done"] else "false",
    }
    if page["next_cursor"] is not None:
        headers["X-Next-Cursor"] = page["next_cursor"]
    return Response(
        content=b"".join(page["lines"]),
        media_type="application/x-ndjson",
        headers=headers,
    )


@app.post(
    "/api/v1/exports/{export_id}/verify", response_model=ExportVerifyResponse
)
def post_export_verify(export_id: str, verify_in: ExportVerifyRequest):
    """提交导出内容摘要以校验完整性；摘要一致时 valid=true。"""
    with db.get_conn() as conn:
        task = _get_export_or_404(conn, export_id)
        valid = exports.verify_manifest(task, verify_in.content_sha256)
        manifest = ExportManifest(**exports.manifest_dict(task))
    return ExportVerifyResponse(
        valid=valid,
        expected_sha256=task.content_sha256,
        received_sha256=verify_in.content_sha256.strip().lower(),
        manifest=manifest,
    )
