"""GET /api/v1/audits/summary：day/week 时间桶汇总、零填充、区间裁剪与 422 校验。

为确定地控制 created_at 落在哪一个时间桶，测试用独立 SQLite 连接直接写入审计行；
WAL 模式下并发只读不冲突，服务端连接立即可见已提交数据。
"""

import sqlite3

import pytest


def _insert_row(db_path, created_at: str, status: str, audit_id: str) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute(
            """
            INSERT INTO audits(
                id, created_at, status, root_plugin, root_version,
                dependency_count, manifest_json, result_json, node_count
            ) VALUES (?, ?, ?, NULL, NULL, 0, '{}', '{}', 0)
            """,
            (audit_id, created_at, status),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def insert_audit(client, tmp_path):
    db_path = tmp_path / "test_audits.db"

    def _insert(created_at: str, status: str, audit_id: str | None = None) -> str:
        audit_id = audit_id or f"audit-{created_at}-{status}".replace(":", "")
        _insert_row(db_path, created_at, status, audit_id)
        return audit_id

    return _insert


def _summarize(client, bucket="day", start=None, end=None):
    params = {"bucket": bucket}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return client.get("/api/v1/audits/summary", params=params)


def test_empty_database_returns_empty_buckets(client):
    resp = _summarize(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bucket"] == "day"
    assert body["start"] is None
    assert body["end"] is None
    assert body["total"] == 0
    assert body["buckets"] == []


def test_day_buckets_zero_fill_and_ascending_order(client, insert_audit):
    # 第 1 天两种状态；第 2 天空桶；第 3 天 inconclusive
    insert_audit("2026-09-20T10:00:00+00:00", "feasible")
    insert_audit("2026-09-20T23:59:59+00:00", "infeasible")
    insert_audit("2026-09-22T00:00:00+00:00", "inconclusive")

    resp = _summarize(
        client, start="2026-09-20T00:00:00Z", end="2026-09-23T00:00:00Z"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3

    buckets = body["buckets"]
    assert [b["bucket_start"] for b in buckets] == [
        "2026-09-20T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
    ]
    assert buckets[0] == {
        "bucket_start": "2026-09-20T00:00:00+00:00",
        "feasible": 1, "infeasible": 1, "inconclusive": 0, "total": 2,
    }
    assert buckets[1] == {
        "bucket_start": "2026-09-21T00:00:00+00:00",
        "feasible": 0, "infeasible": 0, "inconclusive": 0, "total": 0,
    }
    assert buckets[2] == {
        "bucket_start": "2026-09-22T00:00:00+00:00",
        "feasible": 0, "infeasible": 0, "inconclusive": 1, "total": 1,
    }


def test_range_is_left_closed_right_open(client, insert_audit):
    insert_audit("2026-09-19T23:59:59+00:00", "feasible")        # 早于 start
    insert_audit("2026-09-20T12:00:00+00:00", "feasible")        # 区间内
    insert_audit("2026-09-23T00:00:00+00:00", "infeasible")      # 恰好等于 end（右开，排除）

    resp = _summarize(
        client, start="2026-09-20T00:00:00Z", end="2026-09-23T00:00:00Z"
    )
    body = resp.json()
    assert body["total"] == 1
    # end 恰在 09-23T00:00 桶边界：与 [start, end) 相交的尾桶为 09-22；空桶补零
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-20T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
    ]
    assert [b["total"] for b in body["buckets"]] == [1, 0, 0]
    assert body["buckets"][0]["feasible"] == 1


def test_records_outside_range_not_counted_but_intersecting_buckets_zero_filled(
    client, insert_audit
):
    # 全部记录都早于显式区间：记录不计入（total=0），但与区间相交的 09-20 桶补零
    insert_audit("2026-09-01T00:00:00+00:00", "feasible")

    resp = _summarize(
        client, start="2026-09-20T00:00:00Z", end="2026-09-21T00:00:00Z"
    )
    body = resp.json()
    assert body["total"] == 0
    assert [b["bucket_start"] for b in body["buckets"]] == ["2026-09-20T00:00:00+00:00"]
    assert body["buckets"][0]["total"] == 0


def test_week_buckets_start_on_monday_utc(client, insert_audit):
    # 2026-09-20 是周日（属于 09-14 周），09-21 是周一（属于 09-21 周）
    insert_audit("2026-09-20T20:00:00+00:00", "infeasible")
    insert_audit("2026-09-21T08:00:00+00:00", "feasible")
    insert_audit("2026-09-25T08:00:00+00:00", "feasible")  # 周五，同 09-21 周

    resp = _summarize(
        client,
        bucket="week",
        start="2026-09-14T00:00:00Z",
        end="2026-09-28T00:00:00Z",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    buckets = body["buckets"]
    assert [b["bucket_start"] for b in buckets] == [
        "2026-09-14T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
    ]
    assert buckets[0]["infeasible"] == 1
    assert buckets[0]["total"] == 1
    assert buckets[1]["feasible"] == 2
    assert buckets[1]["total"] == 2


def test_week_buckets_zero_fill_empty_weeks(client, insert_audit):
    insert_audit("2026-09-14T00:00:00+00:00", "feasible")
    insert_audit("2026-10-05T00:00:00+00:00", "feasible")  # 第 4 周（09-28/10-05）

    resp = _summarize(
        client,
        bucket="week",
        start="2026-09-14T00:00:00Z",
        end="2026-10-12T00:00:00Z",
    )
    body = resp.json()
    starts = [b["bucket_start"] for b in body["buckets"]]
    assert starts == [
        "2026-09-14T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
        "2026-09-28T00:00:00+00:00",
        "2026-10-05T00:00:00+00:00",
    ]
    assert [b["total"] for b in body["buckets"]] == [1, 0, 0, 1]


def test_bounds_floor_to_bucket_start(client, insert_audit):
    # 起点/终点在桶中间：边界向下对齐；end 所在的桶仍纳入（右开的 end 在该桶内）
    insert_audit("2026-09-20T08:00:00+00:00", "feasible")
    insert_audit("2026-09-22T20:00:00+00:00", "infeasible")

    resp = _summarize(
        client, start="2026-09-20T07:00:00Z", end="2026-09-22T19:00:00Z"
    )
    body = resp.json()
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-20T00:00:00+00:00",
        "2026-09-21T00:00:00+00:00",
        "2026-09-22T00:00:00+00:00",
    ]
    # end=19:00 右开：20:00 的记录被排除，但 09-22 桶仍然出现（补零）
    assert body["buckets"][2]["total"] == 0
    assert body["total"] == 1


def test_default_range_covers_earliest_to_latest_record(client, insert_audit):
    insert_audit("2026-09-15T00:00:00+00:00", "feasible")
    insert_audit("2026-09-17T00:00:00+00:00", "inconclusive")

    resp = _summarize(client)
    body = resp.json()
    assert body["start"] == "2026-09-15T00:00:00+00:00"
    assert body["end"] == "2026-09-17T00:00:00+00:00"
    assert body["total"] == 2
    starts = [b["bucket_start"] for b in body["buckets"]]
    assert starts == [
        "2026-09-15T00:00:00+00:00",
        "2026-09-16T00:00:00+00:00",
        "2026-09-17T00:00:00+00:00",
    ]
    assert [b["total"] for b in body["buckets"]] == [1, 0, 1]


def test_offset_timezones_normalized_to_utc(client, insert_audit):
    # 09-20 01:00+02:00 == 09-19 23:00 UTC，应落在 09-19 的 UTC 日桶
    insert_audit("2026-09-20T01:00:00+02:00", "feasible")

    resp = _summarize(
        client, start="2026-09-19T00:00:00+02:00", end="2026-09-20T02:00:00+02:00"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # start=09-18T22:00Z → 首桶对齐到 09-18（空桶补零）；end=09-20T00:00Z 恰在桶边界 → 尾桶 09-19
    assert [b["bucket_start"] for b in body["buckets"]] == [
        "2026-09-18T00:00:00+00:00",
        "2026-09-19T00:00:00+00:00",
    ]
    assert [b["total"] for b in body["buckets"]] == [0, 1]
    assert body["buckets"][1]["feasible"] == 1


def test_summary_counts_real_api_audits(client):
    for payload in (
        {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        {"dependencies": [
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ]},
    ):
        resp = client.post("/api/v1/audits", json=payload)
        assert resp.status_code == 201

    body = _summarize(client, bucket="day").json()
    assert body["total"] == 2
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["total"] == 2
    assert body["buckets"][0]["feasible"] == 1
    assert body["buckets"][0]["infeasible"] == 1


def test_invalid_time_format_returns_422(client):
    for params in (
        {"start": "not-a-time"},
        {"end": "2026-09-01"},                 # 纯日期、无时间/时区
        {"start": "2026-09-01T00:00:00"},      # 缺时区
        {"start": "2026-09-01T99:00:00Z"},     # 时间分量非法
    ):
        resp = client.get("/api/v1/audits/summary", params=params)
        assert resp.status_code == 422, params


def test_invalid_range_returns_422(client):
    resp = _summarize(
        client, start="2026-09-23T00:00:00Z", end="2026-09-20T00:00:00Z"
    )
    assert resp.status_code == 422

    # start == end（空区间，左闭右开）也非法
    resp = _summarize(
        client, start="2026-09-20T00:00:00Z", end="2026-09-20T00:00:00Z"
    )
    assert resp.status_code == 422


def test_unsupported_bucket_returns_422(client):
    resp = _summarize(client, bucket="month")
    assert resp.status_code == 422


def test_excessive_range_returns_422(client):
    resp = _summarize(
        client, start="1970-01-01T00:00:00Z", end="2026-09-23T00:00:00Z"
    )
    assert resp.status_code == 422
