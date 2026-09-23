"""审计结果的 UTC 时间桶统计（day / week）。

区间为左闭右开 ``[start, end)``；week 桶从周一 UTC 00:00 开始。
本模块只做纯内存计算，不触碰数据库——时间解析与分桶逻辑集中在此，
路由层负责参数校验（非法输入映射为 422），持久化层负责取数。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)

STATUS_KEYS = ("feasible", "infeasible", "inconclusive")


class InvalidTimeBound(ValueError):
    """UTC 时间参数无法解析或不是 UTC 时间。"""


def parse_utc_bound(value: str) -> datetime:
    """解析查询参数中的 UTC 起止时间。

    接受两种形态：

    * ``YYYY-MM-DD``：视为该日 UTC 00:00:00；
    * ISO 8601 日期时间：必须显式携带时区且为 UTC（``Z`` 或 ``+00:00``），
      朴素时间（无时区）与非 UTC 偏移一律拒绝。

    返回带 UTC 时区的 ``datetime``。
    """
    text = value.strip()
    if not text:
        raise InvalidTimeBound("时间不能为空")
    try:
        if len(text) == 10:
            parsed: datetime = datetime.combine(
                date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidTimeBound(
            f"非法 UTC 时间 {value!r}：应为 YYYY-MM-DD 或带 Z/+00:00 的 ISO 8601 时间"
        ) from exc

    if parsed.tzinfo is None:
        raise InvalidTimeBound(f"时间 {value!r} 缺少时区信息，请使用 UTC（Z 或 +00:00）")
    if parsed.utcoffset() != timedelta(0):
        raise InvalidTimeBound(f"时间 {value!r} 不是 UTC 时间（仅接受 Z 或 +00:00 偏移）")
    return parsed.astimezone(timezone.utc)


def floor_to_day(dt: datetime) -> datetime:
    """向下取整到 UTC 当日 00:00。"""
    return dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def floor_to_week(dt: datetime) -> datetime:
    """向下取整到所在周的周一 UTC 00:00（Monday=0）。"""
    day_start = floor_to_day(dt)
    return day_start - timedelta(days=day_start.weekday())


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def build_audit_summary(
    rows: List[Tuple[str, str]],
    *,
    bucket: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """按时间桶汇总审计状态计数。

    ``rows`` 为 ``(created_at_iso, status)`` 序列（可超出区间，内部按
    左闭右开过滤）。``start``/``end`` 均为 UTC datetime 且调用方需保证
    若二者同时给出则 ``start < end``。

    缺省边界（二者均可选）：

    * 库里没有任何记录时返回空结果（``start``/``end`` 回显 null）；
    * 省略 ``start``：取最早记录所在桶的起始时刻；
    * 省略 ``end``：取最晚记录所在桶的下一桶起始时刻（保证最晚记录落入统计）；
      若只给了 ``start`` 而库里无记录，则以当前 UTC 时间兜底。

    桶起始时刻始终对齐到自然边界（day=UTC 零点，week=周一 UTC 零点），
    因此未对齐的 ``start`` 会使首桶早于 ``start``，但桶内只统计区间内记录；
    没有记录的桶补零，结果按桶起始时刻升序。
    """
    if bucket not in ("day", "week"):
        raise ValueError(f"不支持的时间桶: {bucket!r}（仅支持 day/week）")
    width = WEEK if bucket == "week" else DAY
    floor = floor_to_week if bucket == "week" else floor_to_day

    # 统一解析为 UTC datetime（历史记录理论上均带时区，朴素值按 UTC 兜底）
    records: List[Tuple[datetime, str]] = []
    for created_at, status in rows:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        records.append((dt, status))

    if start is None or end is None:
        if not records:
            # 只给 start 而无记录时用 now 兜底，仍可返回一串空桶；其余情况空结果
            if start is not None and end is None:
                end = (now or datetime.now(timezone.utc))
                if start >= end:
                    return _empty_body(bucket, start, None)
            else:
                return {"bucket": bucket, "start": None, "end": None, "total": 0, "buckets": []}
        if start is None:
            start = floor(min(dt for dt, _ in records))
        if end is None:
            end = floor(max(dt for dt, _ in records)) + width
        if start >= end:
            return _empty_body(bucket, start, end)

    counts: Dict[datetime, Dict[str, int]] = {}
    total = 0
    for dt, status in records:
        if not (start <= dt < end):
            continue  # 范围外记录不计入（左闭右开）
        bucket_start = floor(dt)
        slot = counts.setdefault(bucket_start, {key: 0 for key in STATUS_KEYS})
        if status in STATUS_KEYS:
            slot[status] += 1
            total += 1

    buckets: List[Dict[str, Any]] = []
    cursor = floor(start)
    while cursor < end:
        slot = counts.get(cursor, {key: 0 for key in STATUS_KEYS})
        bucket_total = sum(slot[key] for key in STATUS_KEYS)
        buckets.append(
            {
                "bucket_start": cursor.isoformat(),
                "feasible": slot["feasible"],
                "infeasible": slot["infeasible"],
                "inconclusive": slot["inconclusive"],
                "total": bucket_total,
            }
        )
        cursor += width

    return {
        "bucket": bucket,
        "start": _iso(start),
        "end": _iso(end),
        "total": total,
        "buckets": buckets,
    }


def _empty_body(bucket: str, start: datetime, end: Optional[datetime]) -> Dict[str, Any]:
    return {
        "bucket": bucket,
        "start": _iso(start),
        "end": _iso(end),
        "total": 0,
        "buckets": [],
    }
