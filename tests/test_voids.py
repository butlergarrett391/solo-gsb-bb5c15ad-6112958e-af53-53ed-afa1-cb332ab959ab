"""缺包作废测试：作废段校验、前沿推进与排水、告警评估沿用、
幂等重放与冲突、作废记录查询、迟到补交去重、导出 complete 语义。"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(device, seq, key, value=1.0):
    return {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key,
        "measurements": [{"metric": "temperature", "value": value, "unit": "C"}],
    }


def void(client, device, from_seq, to_seq, key):
    return client.post(
        f"/api/v1/devices/{device}/voids",
        json={"from_seq": from_seq, "to_seq": to_seq, "idempotency_key": key},
    )


# ---------------------------------------------------------------------------
# 正常作废：推进前沿 + 立即排水
# ---------------------------------------------------------------------------

def test_void_advances_frontier_and_drains(client):
    # 1、2 提交；4、5 缓冲（缺 3）
    for i in (1, 2, 4, 5):
        client.post("/api/v1/packages", json=pkg("void-a", i, f"va-{i}"))
    gaps = client.get("/api/v1/devices/void-a/gaps").json()
    assert gaps["next_expected"] == 3 and gaps["buffered_seqs"] == [4, 5]

    r = void(client, "void-a", 3, 3, "va-void-3")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "voided"
    assert body["from_seq"] == 3 and body["to_seq"] == 3
    assert body["committed_seqs"] == [4, 5]  # 立即排水随后连续的缓冲包
    assert body["committed_upto"] == 5

    # 作废序号不进入已提交；缓冲区清空
    committed = client.get("/api/v1/devices/void-a/committed").json()
    assert committed["seqs"] == [1, 2, 4, 5]
    assert committed["committed_upto"] == 5
    gaps = client.get("/api/v1/devices/void-a/gaps").json()
    assert gaps["buffered_seqs"] == [] and gaps["gaps"] == []

    # 作废记录可查
    voids = client.get("/api/v1/devices/void-a/voids").json()
    assert voids["count"] == 1
    rec = voids["records"][0]
    assert rec["device_id"] == "void-a"
    assert rec["from_seq"] == 3 and rec["to_seq"] == 3
    assert rec["idempotency_key"] == "va-void-3"
    assert rec["created_at"]


def test_void_multi_seq_segment(client):
    client.post("/api/v1/packages", json=pkg("void-b", 1, "vb-1"))
    client.post("/api/v1/packages", json=pkg("void-b", 5, "vb-5"))
    client.post("/api/v1/packages", json=pkg("void-b", 6, "vb-6"))
    r = void(client, "void-b", 2, 4, "vb-void")
    assert r.status_code == 200
    body = r.json()
    assert body["committed_seqs"] == [5, 6]
    assert body["committed_upto"] == 6
    committed = client.get("/api/v1/devices/void-b/committed").json()
    assert committed["seqs"] == [1, 5, 6]


def test_void_without_drain_when_next_not_buffered(client):
    # 缓冲 {9}，作废 [4,5]：前沿推进到 5，但 6 不在缓冲区，不排水
    for i in (1, 2, 3):
        client.post("/api/v1/packages", json=pkg("void-c", i, f"vc-{i}"))
    client.post("/api/v1/packages", json=pkg("void-c", 9, "vc-9"))
    r = void(client, "void-c", 4, 5, "vc-void")
    assert r.status_code == 200
    body = r.json()
    assert body["committed_seqs"] == []
    assert body["committed_upto"] == 5
    gaps = client.get("/api/v1/devices/void-c/gaps").json()
    assert gaps["next_expected"] == 6
    assert gaps["buffered_seqs"] == [9]
    assert gaps["gaps"] == [6, 7, 8]


def test_void_chained_segments(client):
    # 分多段作废，逐段推进，最后一段放行缓冲包
    client.post("/api/v1/packages", json=pkg("void-k", 1, "vk-1"))
    client.post("/api/v1/packages", json=pkg("void-k", 10, "vk-10"))
    assert void(client, "void-k", 2, 3, "vk-v1").json()["committed_upto"] == 3
    assert void(client, "void-k", 4, 5, "vk-v2").json()["committed_upto"] == 5
    r = void(client, "void-k", 6, 9, "vk-v3").json()
    assert r["committed_seqs"] == [10] and r["committed_upto"] == 10

    voids = client.get("/api/v1/devices/void-k/voids").json()
    assert voids["count"] == 3
    assert [(x["from_seq"], x["to_seq"]) for x in voids["records"]] == [
        (2, 3), (4, 5), (6, 9),
    ]
    # 分页
    page = client.get(
        "/api/v1/devices/void-k/voids", params={"limit": 2, "offset": 2}
    ).json()
    assert page["count"] == 3 and len(page["records"]) == 1
    assert page["records"][0]["from_seq"] == 6


# ---------------------------------------------------------------------------
# 校验与错误
# ---------------------------------------------------------------------------

def test_void_validation_errors(client):
    client.post("/api/v1/packages", json=pkg("void-d", 1, "vd-1"))
    client.post("/api/v1/packages", json=pkg("void-d", 4, "vd-4"))  # 缓冲，缺 2、3

    # 未知设备 -> 404
    assert void(client, "ghost", 1, 1, "vd-ghost").status_code == 404
    # 缺幂等键 -> 422
    r = client.post("/api/v1/devices/void-d/voids", json={"from_seq": 2, "to_seq": 3})
    assert r.status_code == 422
    # from_seq > to_seq（请求体校验）-> 422
    assert void(client, "void-d", 3, 2, "vd-v0").status_code == 422
    # 起点已提交 -> 422
    r = void(client, "void-d", 1, 2, "vd-v1")
    assert r.status_code == 422 and "已提交" in r.json()["detail"]
    # 起点跳过缺口（不从 next_expected 开始）-> 422
    r = void(client, "void-d", 3, 3, "vd-v2")
    assert r.status_code == 422 and "next_expected" in r.json()["detail"]
    # 段尾越过缓冲区最大序号 -> 422
    r = void(client, "void-d", 2, 5, "vd-v3")
    assert r.status_code == 422 and "缓冲区最大序号" in r.json()["detail"]
    # 段内含已缓冲序号 -> 422
    r = void(client, "void-d", 2, 4, "vd-v4")
    assert r.status_code == 422 and "已在缓冲区" in r.json()["detail"]

    # 失败的请求不留任何痕迹：无作废记录、前沿未动、幂等键未占用
    assert client.get("/api/v1/devices/void-d/voids").json()["count"] == 0
    assert client.get("/api/v1/devices/void-d/gaps").json()["next_expected"] == 2
    r = void(client, "void-d", 2, 3, "vd-v1")  # 复用上面的键 + 合法载荷 -> 成功
    assert r.status_code == 200 and r.json()["status"] == "voided"


def test_void_empty_buffer_rejected(client):
    client.post("/api/v1/packages", json=pkg("void-e", 1, "ve-1"))
    r = void(client, "void-e", 2, 2, "ve-void")
    assert r.status_code == 422
    assert client.get("/api/v1/devices/void-e/voids").json()["count"] == 0


def test_voids_query_unknown_device_404(client):
    assert client.get("/api/v1/devices/ghost/voids").status_code == 404


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_void_idempotent_replay(client):
    client.post("/api/v1/packages", json=pkg("void-f", 1, "vf-1"))
    client.post("/api/v1/packages", json=pkg("void-f", 3, "vf-3"))
    body = {"from_seq": 2, "to_seq": 2, "idempotency_key": "vf-void"}
    first = client.post("/api/v1/devices/void-f/voids", json=body).json()
    assert first["status"] == "voided" and first["committed_upto"] == 3

    replay = client.post("/api/v1/devices/void-f/voids", json=body).json()
    assert replay["status"] == "duplicate"
    assert replay["duplicate_reason"] == "idempotent_replay"
    assert replay["committed_upto"] == 3
    assert replay["committed_seqs"] == [3]

    # 不重复推进、不重复登记；重放也不记入重复包
    assert client.get("/api/v1/devices/void-f/voids").json()["count"] == 1
    assert client.get("/api/v1/devices/void-f/committed").json()["committed_upto"] == 3
    assert client.get("/api/v1/devices/void-f/duplicates").json()["count"] == 0


def test_void_same_key_different_payload_conflict(client):
    client.post("/api/v1/packages", json=pkg("void-g", 1, "vg-1"))
    client.post("/api/v1/packages", json=pkg("void-g", 4, "vg-4"))
    assert void(client, "void-g", 2, 2, "vg-void").status_code == 200
    # 同键不同载荷 -> 409
    assert void(client, "void-g", 2, 3, "vg-void").status_code == 409
    assert void(client, "void-g", 3, 3, "vg-void").status_code == 409


def test_void_idempotency_key_via_header(client):
    client.post("/api/v1/packages", json=pkg("void-m", 1, "vm-1"))
    client.post("/api/v1/packages", json=pkg("void-m", 3, "vm-3"))
    r = client.post(
        "/api/v1/devices/void-m/voids",
        json={"from_seq": 2, "to_seq": 2},
        headers={"Idempotency-Key": "vm-void"},
    )
    assert r.status_code == 200 and r.json()["status"] == "voided"
    # 请求头键同样支持重放
    replay = client.post(
        "/api/v1/devices/void-m/voids",
        json={"from_seq": 2, "to_seq": 2},
        headers={"Idempotency-Key": "vm-void"},
    )
    assert replay.json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# 与摄入语义的衔接
# ---------------------------------------------------------------------------

def test_late_package_after_void_is_duplicate(client):
    client.post("/api/v1/packages", json=pkg("void-i", 1, "vi-1"))
    client.post("/api/v1/packages", json=pkg("void-i", 3, "vi-3"))
    assert void(client, "void-i", 2, 2, "vi-void").status_code == 200
    # 作废序号 2 的包迟到补交 -> 已越过前沿，按 already_committed 去重
    r = client.post("/api/v1/packages", json=pkg("void-i", 2, "vi-2"))
    assert r.json()["status"] == "duplicate"
    assert r.json()["duplicate_reason"] == "already_committed"
    # 后续正常包继续按序提交
    r = client.post("/api/v1/packages", json=pkg("void-i", 4, "vi-4"))
    assert r.json()["status"] == "committed" and r.json()["committed_upto"] == 4


def test_void_drain_uses_alert_evaluation(client):
    # 规则：温度 > 10 连续 1 次触发
    assert client.post(
        "/api/v1/rules",
        json={"device_id": "void-h", "metric": "temperature", "upper_limit": 10},
    ).status_code == 201
    client.post("/api/v1/packages", json=pkg("void-h", 1, "vh-1", value=5.0))
    client.post("/api/v1/packages", json=pkg("void-h", 3, "vh-3", value=20.0))  # 缓冲
    r = void(client, "void-h", 2, 2, "vh-void")
    assert r.json()["committed_seqs"] == [3]

    # 排水的真实包按提交顺序评估：seq 3 触发告警；作废序号 2 不产生任何事件
    events = client.get("/api/v1/devices/void-h/alerts/events").json()
    assert events["count"] == 1
    ev = events["events"][0]
    assert ev["event_type"] == "triggered"
    assert ev["seq"] == 3 and ev["value"] == 20.0
    alerts_now = client.get("/api/v1/alerts", params={"device_id": "void-h"}).json()
    assert alerts_now["count"] == 1
    assert alerts_now["alerts"][0]["triggered_seq"] == 3


# ---------------------------------------------------------------------------
# 导出：仅含真实已提交包，范围含作废序号时不得报告完整
# ---------------------------------------------------------------------------

def test_export_with_voided_range_not_complete(client):
    client.post("/api/v1/packages", json=pkg("void-j", 1, "vj-1", value=1.0))
    client.post("/api/v1/packages", json=pkg("void-j", 3, "vj-3", value=3.0))
    assert void(client, "void-j", 2, 2, "vj-void").status_code == 200
    # committed = {1, 3}，序号 2 已作废

    # 全量导出：只含真实已提交包；范围 [1,3] 含作废序号 2 -> complete=False
    m = client.post("/api/v1/exports", json={"device_id": "void-j"}).json()
    assert m["package_count"] == 2
    assert m["first_seq"] == 1 and m["last_seq"] == 3
    assert m["complete"] is False
    r = client.get(f"/api/v1/exports/{m['export_id']}/pages")
    assert r.status_code == 200
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert [p["seq"] for p in lines] == [1, 3]
    assert hashlib.sha256(r.content).hexdigest() == m["content_sha256"]

    # 显式范围含作废序号 -> 同样不完整
    m2 = client.post(
        "/api/v1/exports",
        json={"device_id": "void-j", "from_seq": 1, "to_seq": 3},
    ).json()
    assert m2["package_count"] == 2 and m2["complete"] is False

    # 范围避开作废序号 -> 完整
    m3 = client.post(
        "/api/v1/exports",
        json={"device_id": "void-j", "from_seq": 3, "to_seq": 3},
    ).json()
    assert m3["package_count"] == 1 and m3["complete"] is True
