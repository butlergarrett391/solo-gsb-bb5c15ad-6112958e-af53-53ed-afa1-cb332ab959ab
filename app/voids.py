"""设备缺包作废：确认丢失的连续序号段登记作废，推进顺序前沿并放行滞留缓冲包。

语义（按设备独立维护，与摄入共用 ingest_lock 与同一事务）：

- 仅允许对**已有设备**、从 next_expected（committed_upto + 1）开始的连续
  序号段 [from_seq, to_seq] 发起作废；段内序号当前必须确为缺口——
  已提交或已缓冲的序号不得作废；
- 段尾 to_seq 不得超过当前缓冲区最大序号：作废的意义是桥接缺口、放行其后
  滞留的缓冲包，缓冲为空或段尾越界都说明没有可放行的数据；
- 登记作废记录后前沿推进到段尾，并立即从段尾 + 1 起连续排水提交缓冲包
  （与摄入共用 drain_buffer）；排水沿用摄入的告警评估，每个真实包恰好评估
  一次；作废序号本身不是测量包——不产生 committed 行、不参与告警评估、
  不进入导出内容；
- 作废请求携带幂等键：相同请求重放返回首次处理结果（status=duplicate），
  不重复推进前沿、不重复登记记录；同一幂等键携带不同载荷返回 409。
"""

import hashlib
import json
import sqlite3

from . import alerts
from .logic import IdempotencyConflict, drain_buffer
from .schemas import VoidResponse


class VoidError(ValueError):
    """作废请求非法（起点不是 next_expected、段尾越过缓冲区最大序号、
    段内含已缓冲序号等）。"""


def _canonical_payload(device_id: str, from_seq: int, to_seq: int) -> str:
    return json.dumps(
        {"device_id": device_id, "from_seq": from_seq, "to_seq": to_seq},
        sort_keys=True,
        ensure_ascii=False,
    )


def _payload_hash(device_id: str, from_seq: int, to_seq: int) -> str:
    return hashlib.sha256(
        _canonical_payload(device_id, from_seq, to_seq).encode("utf-8")
    ).hexdigest()


def _store_key(
    conn: sqlite3.Connection,
    key: str,
    device_id: str,
    from_seq: int,
    to_seq: int,
    payload_hash: str,
    response: VoidResponse,
) -> None:
    # 与摄入共用 idempotency_keys 表（同一键命名空间）；seq 列登记段起点
    conn.execute(
        "INSERT INTO idempotency_keys (key, device_id, seq, payload_hash, response_json)"
        " VALUES (?,?,?,?,?)",
        (key, device_id, from_seq, payload_hash, response.model_dump_json()),
    )


def void_range(
    conn: sqlite3.Connection,
    device_id: str,
    from_seq: int,
    to_seq: int,
    key: str,
) -> VoidResponse:
    """在单个事务内完成一次作废。调用方需持有 ingest_lock 并保证设备存在。"""
    payload_hash = _payload_hash(device_id, from_seq, to_seq)

    # 1) 幂等键重放：直接返回首次处理的结果，不重复推进、不重复登记
    row = conn.execute(
        "SELECT payload_hash, response_json FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is not None:
        if row["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                f"idempotency key {key!r} 已被不同载荷使用"
            )
        stored = VoidResponse.model_validate_json(row["response_json"])
        return stored.model_copy(
            update={"status": "duplicate", "duplicate_reason": "idempotent_replay"}
        )

    # 2) 校验作废段：必须从 next_expected 开始的连续缺口，段尾不越过缓冲区最大序号
    committed_upto = conn.execute(
        "SELECT committed_upto FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()["committed_upto"]
    next_expected = committed_upto + 1
    if from_seq <= committed_upto:
        raise VoidError(
            f"序号 {from_seq} 已提交，不得作废；"
            f"作废段必须从 next_expected={next_expected} 开始"
        )
    if from_seq > next_expected:
        raise VoidError(
            f"作废段必须从 next_expected={next_expected} 开始，不得跳过缺口"
        )
    max_buffered = conn.execute(
        "SELECT MAX(seq) AS m FROM buffer WHERE device_id = ?", (device_id,)
    ).fetchone()["m"]
    if max_buffered is None:
        raise VoidError("缓冲区为空，没有待放行的缺口，无需作废")
    if to_seq > max_buffered:
        raise VoidError(
            f"段尾 to_seq={to_seq} 不得超过当前缓冲区最大序号 {max_buffered}"
        )
    blocked = [
        r["seq"]
        for r in conn.execute(
            "SELECT seq FROM buffer WHERE device_id = ? AND seq BETWEEN ? AND ?"
            " ORDER BY seq",
            (device_id, from_seq, to_seq),
        ).fetchall()
    ]
    if blocked:
        raise VoidError(f"序号 {blocked} 已在缓冲区，不得作废")

    # 3) 登记作废记录
    conn.execute(
        "INSERT INTO voids (device_id, from_seq, to_seq, idempotency_key)"
        " VALUES (?,?,?,?)",
        (device_id, from_seq, to_seq, key),
    )

    # 4) 前沿推进到段尾，随后立即排水提交随后连续的缓冲包。
    #    告警仅对排水的真实测量包评估，作废序号不伪装成测量包。
    committed_seqs, committed_payloads, committed_upto = drain_buffer(
        conn, device_id, to_seq + 1
    )
    conn.execute(
        "UPDATE devices SET committed_upto = ? WHERE device_id = ?",
        (committed_upto, device_id),
    )
    alerts.evaluate_drain(conn, device_id, committed_payloads)

    response = VoidResponse(
        status="voided",
        device_id=device_id,
        from_seq=from_seq,
        to_seq=to_seq,
        committed_upto=committed_upto,
        committed_seqs=committed_seqs,
    )
    _store_key(conn, key, device_id, from_seq, to_seq, payload_hash, response)
    return response
