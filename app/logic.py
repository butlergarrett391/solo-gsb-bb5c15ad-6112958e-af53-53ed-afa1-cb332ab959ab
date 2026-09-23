"""核心摄入逻辑：幂等去重、乱序缓冲、缺口补齐后按序提交。

提交规则（按设备独立维护）：
- 每台设备记录 committed_upto = 已连续提交的最大序号；
- 设备首个到达的包用于自举：committed_upto 初始化为 seq - 1，即首包立即提交；
- 之后到达的包：seq <= committed_upto 视为重复；已在缓冲区视为重复；
- 否则放入缓冲区，随后从 committed_upto + 1 起连续"排水"提交。

committed_upto 也可由缺包作废推进（见 voids.py）：作废段被前沿永久越过，
不再期望到达，其迟到补交按 already_committed 去重。
"""

import hashlib
import json
import sqlite3
from typing import List, Optional, Tuple

from . import alerts
from .schemas import IngestResponse, PackageIn


class IdempotencyConflict(Exception):
    """同一幂等键携带了不同的载荷。"""


def _canonical_payload(pkg: PackageIn) -> str:
    return json.dumps(
        {
            "device_id": pkg.device_id,
            "seq": pkg.seq,
            "sent_at": pkg.sent_at.isoformat() if pkg.sent_at else None,
            "measurements": [m.model_dump(mode="json") for m in pkg.measurements],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _payload_hash(pkg: PackageIn) -> str:
    return hashlib.sha256(_canonical_payload(pkg).encode("utf-8")).hexdigest()


def _record_duplicate(
    conn: sqlite3.Connection,
    device_id: str,
    seq: Optional[int],
    key: Optional[str],
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO duplicates (device_id, seq, idempotency_key, reason) VALUES (?,?,?,?)",
        (device_id, seq, key, reason),
    )


def _store_key(
    conn: sqlite3.Connection,
    key: str,
    pkg: PackageIn,
    payload_hash: str,
    response: IngestResponse,
) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (key, device_id, seq, payload_hash, response_json)"
        " VALUES (?,?,?,?,?)",
        (key, pkg.device_id, pkg.seq, payload_hash, response.model_dump_json()),
    )


def drain_buffer(
    conn: sqlite3.Connection, device_id: str, start: int
) -> Tuple[List[int], List[tuple], int]:
    """从 start 起连续"排水"提交缓冲包，返回 (committed_seqs, committed_payloads, 新前沿)。

    committed_payloads 为按提交顺序排列的 (seq, payload_json)，供告警评估回放；
    摄入与缺包作废共用本函数，保证两条路径的排水行为完全一致。
    """
    committed_seqs: List[int] = []
    committed_payloads: List[tuple] = []
    cur = start
    while True:
        row = conn.execute(
            "SELECT payload_json FROM buffer WHERE device_id = ? AND seq = ?",
            (device_id, cur),
        ).fetchone()
        if row is None:
            break
        conn.execute(
            "INSERT INTO committed (device_id, seq, payload_json) VALUES (?,?,?)",
            (device_id, cur, row["payload_json"]),
        )
        conn.execute(
            "DELETE FROM buffer WHERE device_id = ? AND seq = ?", (device_id, cur)
        )
        committed_seqs.append(cur)
        committed_payloads.append((cur, row["payload_json"]))
        cur += 1
    return committed_seqs, committed_payloads, cur - 1


def ingest_package(conn: sqlite3.Connection, pkg: PackageIn, key: str) -> IngestResponse:
    """在单个事务内完成一次摄入。调用方需持有 ingest_lock。"""
    payload_hash = _payload_hash(pkg)

    # 1) 幂等键重放：直接返回首次处理的结果
    row = conn.execute(
        "SELECT payload_hash, response_json FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row is not None:
        if row["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                f"idempotency key {key!r} 已被不同载荷使用"
            )
        _record_duplicate(conn, pkg.device_id, pkg.seq, key, "idempotent_replay")
        stored = IngestResponse.model_validate_json(row["response_json"])
        return stored.model_copy(update={"status": "duplicate", "duplicate_reason": "idempotent_replay"})

    # 2) 取设备状态；不存在则以首包自举
    dev = conn.execute(
        "SELECT committed_upto FROM devices WHERE device_id = ?", (pkg.device_id,)
    ).fetchone()
    if dev is None:
        committed_upto = pkg.seq - 1
        conn.execute(
            "INSERT INTO devices (device_id, committed_upto) VALUES (?,?)",
            (pkg.device_id, committed_upto),
        )
    else:
        committed_upto = dev["committed_upto"]

    # 3) 重复包判定
    duplicate_reason: Optional[str] = None
    if pkg.seq <= committed_upto:
        duplicate_reason = "already_committed"
    else:
        in_buffer = conn.execute(
            "SELECT 1 FROM buffer WHERE device_id = ? AND seq = ?",
            (pkg.device_id, pkg.seq),
        ).fetchone()
        if in_buffer is not None:
            duplicate_reason = "already_buffered"

    if duplicate_reason is not None:
        _record_duplicate(conn, pkg.device_id, pkg.seq, key, duplicate_reason)
        response = IngestResponse(
            status="duplicate",
            device_id=pkg.device_id,
            seq=pkg.seq,
            committed_upto=committed_upto,
            committed_seqs=[],
            duplicate_reason=duplicate_reason,
        )
        _store_key(conn, key, pkg, payload_hash, response)
        return response

    # 4) 入缓冲区，随后从下一个期望序号起连续排水提交
    payload_json = _canonical_payload(pkg)
    conn.execute(
        "INSERT INTO buffer (device_id, seq, payload_json) VALUES (?,?,?)",
        (pkg.device_id, pkg.seq, payload_json),
    )

    committed_seqs, committed_payloads, committed_upto = drain_buffer(
        conn, pkg.device_id, committed_upto + 1
    )
    conn.execute(
        "UPDATE devices SET committed_upto = ? WHERE device_id = ?",
        (committed_upto, pkg.device_id),
    )

    # 阈值告警：仅对本次按序排水的包评估，每个包恰好一次。
    # 规则在包创建之后才建立时不回溯历史提交（历史已按当时规则集处理完毕）。
    alerts.evaluate_drain(conn, pkg.device_id, committed_payloads)

    response = IngestResponse(
        status="committed" if pkg.seq in committed_seqs else "buffered",
        device_id=pkg.device_id,
        seq=pkg.seq,
        committed_upto=committed_upto,
        committed_seqs=committed_seqs,
    )
    _store_key(conn, key, pkg, payload_hash, response)
    return response
