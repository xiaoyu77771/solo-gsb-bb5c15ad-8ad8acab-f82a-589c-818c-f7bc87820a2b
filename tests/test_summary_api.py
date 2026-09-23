"""GET /api/v1/audits/summary 时间桶汇总的端到端测试。

历史审计的 created_at 无法通过提交接口控制，因此除走 API 的场景外，
直接用 insert_audit 写入指定创建时刻的记录。
"""

import uuid

from app.database import insert_audit

_STATUSES = ("feasible", "infeasible", "inconclusive")


def _insert(client, created_at: str, status: str) -> str:
    assert status in _STATUSES
    conn = client.app.state.conn
    audit_id = str(uuid.uuid4())
    insert_audit(
        conn,
        audit_id=audit_id,
        created_at=created_at,
        status=status,
        root_plugin=None,
        root_version=None,
        dependency_count=0,
        manifest={"dependencies": []},
        result={
            "id": audit_id,
            "created_at": created_at,
            "status": status,
            "root_plugin": None,
            "root_version": None,
            "explanation": "",
            "node_count": 0,
            "resolution": None,
            "sources": None,
            "conflicts": [],
        },
        node_count=0,
    )
    return audit_id


def _summary(client, **params):
    return client.get("/api/v1/audits/summary", params=params)


def test_day_buckets_zero_fill_half_open(client):
    # 2026-09-21 是周一
    _insert(client, "2026-09-21T08:00:00+00:00", "feasible")
    _insert(client, "2026-09-21T23:59:00+00:00", "infeasible")
    _insert(client, "2026-09-22T10:00:00+00:00", "feasible")
    _insert(client, "2026-09-24T12:00:00+00:00", "inconclusive")  # == end，右开排除

    resp = _summary(client, bucket="day", start="2026-09-21", end="2026-09-24")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bucket"] == "day"
    assert body["start"] == "2026-09-21T00:00:00+00:00"
    assert body["end"] == "2026-09-24T00:00:00+00:00"
    assert body["total"] == 3

    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
        "2026-09-23T00:00:00+00:00",  # 空桶补零
    ]
    first, second, third = body["buckets"]
    assert (first["feasible"], first["infeasible"], first["inconclusive"], first["total"]) == (1, 1, 0, 2)
    assert (second["feasible"], second["total"]) == (1, 1)
    assert (third["feasible"], third["infeasible"], third["inconclusive"], third["total"]) == (0, 0, 0, 0)


def test_start_boundary_included_end_boundary_excluded(client):
    _insert(client, "2026-09-21T10:00:00+00:00", "feasible")  # == start，左闭计入
    _insert(client, "2026-09-22T10:00:00+00:00", "feasible")  # == end，右开排除

    resp = _summary(
        client,
        bucket="day",
        start="2026-09-21T10:00:00Z",
        end="2026-09-22T10:00:00Z",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    # start/end 未对齐桶边界：桶仍按自然日铺开，覆盖到 end 所在日（空桶补零）
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
    ]
    assert body["buckets"][0]["total"] == 1
    assert body["buckets"][1]["total"] == 0  # 恰在 end 的记录被右开排除


def test_week_buckets_start_on_monday_utc(client):
    # 2026-09-21 周一，2026-09-23 周三（落在 9-21 起的同一周）
    _insert(client, "2026-09-23T09:00:00+00:00", "infeasible")
    _insert(client, "2026-10-01T09:00:00+00:00", "inconclusive")  # 9-28 起的下一周

    resp = _summary(client, bucket="week", start="2026-09-21", end="2026-10-05")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-21T00:00:00+00:00",  # 周一
        "2026-09-28T00:00:00+00:00",  # 下一个周一
    ]
    assert body["buckets"][0]["infeasible"] == 1
    assert body["buckets"][0]["total"] == 1
    assert body["buckets"][1]["inconclusive"] == 1
    assert body["total"] == 2


def test_week_buckets_floor_unaligned_start_to_monday(client):
    # 从周三开始查询：首桶仍回退到周一，但区间外（周一、周二）的记录不计入
    _insert(client, "2026-09-21T00:00:00+00:00", "feasible")  # 周一，范围外
    _insert(client, "2026-09-23T09:00:00+00:00", "feasible")  # 周三，范围内

    body = _summary(client, bucket="week", start="2026-09-23", end="2026-10-05").json()
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-21T00:00:00+00:00",
        "2026-09-28T00:00:00+00:00",
    ]
    first_bucket = body["buckets"][0]
    assert first_bucket["feasible"] == 1
    assert first_bucket["total"] == 1
    assert body["total"] == 1


def test_invalid_time_formats_return_422(client):
    bad_params = [
        {"start": "not-a-time"},
        {"end": "2026-13-99"},
        {"start": "2026-09-21T00:00:00"},          # 朴素时间，无时区
        {"start": "2026-09-21T00:00:00+03:00"},    # 非 UTC 偏移
        {"start": "2026-09-21T00:00:00-05:00"},    # 非 UTC 偏移
        {"start": "2026-09-22", "end": "2026-09-21"},  # start >= end
        {"start": "2026-09-21T00:00:00Z", "end": "2026-09-21T00:00:00Z"},  # 相等
    ]
    for params in bad_params:
        resp = _summary(client, **params)
        assert resp.status_code == 422, params


def test_invalid_bucket_returns_422(client):
    resp = _summary(client, bucket="month", start="2026-09-01", end="2026-09-30")
    assert resp.status_code == 422


def test_empty_database_defaults_to_empty_result(client):
    resp = _summary(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"bucket": "day", "start": None, "end": None, "total": 0, "buckets": []}


def test_default_bounds_derived_from_records(client):
    _insert(client, "2026-09-21T08:00:00+00:00", "feasible")
    _insert(client, "2026-09-23T20:00:00+00:00", "infeasible")

    body = _summary(client, bucket="day").json()
    assert body["start"] == "2026-09-21T00:00:00+00:00"
    assert body["end"] == "2026-09-24T00:00:00+00:00"  # 最晚桶的下一桶起始
    assert body["total"] == 2
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
        "2026-09-23T00:00:00+00:00",
    ]
    # 升序
    starts = [b["bucket_start"] for b in body["buckets"]]
    assert starts == sorted(starts)


def test_default_week_bounds_floor_to_monday(client):
    _insert(client, "2026-09-23T08:00:00+00:00", "feasible")  # 周三
    body = _summary(client, bucket="week").json()
    assert body["start"] == "2026-09-21T00:00:00+00:00"  # 回退到周一
    assert body["end"] == "2026-09-28T00:00:00+00:00"
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["total"] == 1


def test_only_start_without_records_returns_zero_buckets(client):
    body = _summary(client, bucket="day", start="2026-09-01").json()
    assert body["total"] == 0
    assert body["buckets"], "给定 start 时应从 start 向当前时间补空桶"
    assert all(b["total"] == 0 for b in body["buckets"])
    assert body["start"] == "2026-09-01T00:00:00+00:00"
    assert body["end"] is not None


def test_summary_route_not_shadowed_by_detail_route(client):
    # /audits/summary 必须命中汇总接口而不是被 /audits/{id} 当作 ID
    resp = client.get("/api/v1/audits/summary")
    assert resp.status_code == 200
    assert "buckets" in resp.json()
    # 真正不存在的 ID 仍是 404
    assert client.get("/api/v1/audits/no-such-id").status_code == 404


def test_records_created_via_api_are_counted(client):
    resp = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
    )
    assert resp.status_code == 201
    created_at = resp.json()["created_at"]
    day = created_at[:10]

    body = _summary(client, bucket="day", start=day).json()
    today = next(b for b in body["buckets"] if b["bucket_start"].startswith(day))
    assert today["feasible"] >= 1
    assert today["total"] >= 1
    assert body["total"] >= 1
