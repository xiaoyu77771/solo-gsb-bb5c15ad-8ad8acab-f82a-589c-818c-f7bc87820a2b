"""审计汇总的时间桶聚合：day/week 分桶、零填充与 UTC 区间裁剪（纯函数，不触碰数据库）。

约定：

* 起止时间为带时区的 ISO 8601 时间戳（如 ``2026-09-01T00:00:00Z``），统一归一到 UTC；
* 区间为**左闭右开** ``[start, end)``：``created_at >= end`` 的记录不计入；
* ``day`` 桶从 UTC 00:00 开始，``week`` 桶从 UTC 周一 00:00 开始；
* 起止边界分别向下对齐到所在桶的起点，覆盖范围内的空桶补零；
* 省略边界时回退到最早/最晚一条审计记录所在桶；无任何记录时返回空桶列表。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Literal, Optional, Tuple

BucketSize = Literal["day", "week"]

# 单次汇总允许的最大桶数（day≈27 年 / week≈192 年），防止病态区间撑爆响应
MAX_BUCKETS = 10_000

_BUCKET_STEP: Dict[BucketSize, timedelta] = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
}

_STATUSES = ("feasible", "infeasible", "inconclusive")


class TimeRangeError(ValueError):
    """时间格式或区间非法（路由层映射为 422）。"""


def parse_utc_datetime(raw: str, param: str) -> datetime:
    """解析带时区的 ISO 8601 时间戳并归一到 UTC；格式非法或缺时区抛 TimeRangeError。"""
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeRangeError(f"参数 {param} 时间格式非法: {raw!r}（需 ISO 8601，如 2026-09-01T00:00:00Z）") from exc
    if parsed.tzinfo is None:
        raise TimeRangeError(f"参数 {param} 缺少时区信息: {raw!r}（需显式 UTC 时区，如后缀 Z 或 +00:00）")
    return parsed.astimezone(timezone.utc)


def bucket_floor(moment: datetime, bucket: BucketSize) -> datetime:
    """把 UTC 时刻向下对齐到所在桶的起点（day=当日 00:00，week=所在周周一 00:00）。"""
    day = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    if bucket == "week":
        day -= timedelta(days=day.weekday())  # weekday(): 周一为 0
    return day


def _parse_created_at(raw: str) -> Optional[datetime]:
    """解析记录上的 created_at（本服务写入的均为 UTC ISO 格式）；异常值返回 None 跳过。"""
    try:
        return parse_utc_datetime(raw, "created_at")
    except TimeRangeError:
        return None


def build_time_summary(
    rows: Iterable[Tuple[str, str]],
    bucket: BucketSize,
    start: Optional[datetime],
    end: Optional[datetime],
) -> Tuple[List[Dict[str, object]], Optional[datetime], Optional[datetime]]:
    """按桶聚合 (created_at, status) 行，返回 (buckets, effective_start, effective_end)。

    * ``rows`` 元素为 (ISO 时间戳, 状态)；无法解析的时间戳会被忽略；
    * 返回的桶按起始时刻升序，空桶三项计数均为 0；
    * effective_start/end 为实际生效的边界（省略时回退到记录范围），供响应回显。
    """
    if start is not None and end is not None and start >= end:
        raise TimeRangeError(
            f"时间区间非法：start（{start.isoformat()}）必须早于 end（{end.isoformat()}）"
        )

    step = _BUCKET_STEP[bucket]

    # 桶内计数：{桶起点: {状态: 数量}}；范围外的记录直接丢弃
    counts: Dict[datetime, Dict[str, int]] = {}
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    for created_raw, status in rows:
        moment = _parse_created_at(created_raw)
        if moment is None:
            continue
        if earliest is None or moment < earliest:
            earliest = moment
        if latest is None or moment > latest:
            latest = moment
        if start is not None and moment < start:
            continue
        if end is not None and moment >= end:  # 左闭右开：end 本身不计入
            continue
        if status not in _STATUSES:
            continue
        key = bucket_floor(moment, bucket)
        bucket_counts = counts.setdefault(key, {name: 0 for name in _STATUSES})
        bucket_counts[status] += 1

    # 省略的边界回退到记录范围（记录都没有则无法成桶，返回空）
    effective_start = start if start is not None else earliest
    effective_end = end if end is not None else latest
    if effective_start is None or effective_end is None:
        return [], None, None

    first = bucket_floor(effective_start, bucket)
    if end is not None:
        # 右开区间：与 [start, end) 相交的最后一个桶满足 k < end；
        # end 恰好落在桶边界（floor(end) == end）时，尾桶回退一格
        last = bucket_floor(effective_end, bucket)
        if last >= effective_end:
            last -= step
    else:
        # 省略 end 时 effective_end 是最晚的在档记录（含），其所在桶必须保留
        last = bucket_floor(effective_end, bucket)
    if last < first:
        # 仅当记录全部落在显式边界之外时才会出现（如 start 晚于全部记录）
        return [], effective_start, effective_end

    bucket_total = int((last - first) / step) + 1
    if bucket_total > MAX_BUCKETS:
        raise TimeRangeError(
            f"时间区间过大：按 {bucket} 分桶将产生 {bucket_total} 个桶（上限 {MAX_BUCKETS}），请缩小范围"
        )

    buckets: List[Dict[str, object]] = []
    cursor = first
    while cursor <= last:
        bucket_counts = counts.get(cursor, {})
        feasible = int(bucket_counts.get("feasible", 0))
        infeasible = int(bucket_counts.get("infeasible", 0))
        inconclusive = int(bucket_counts.get("inconclusive", 0))
        buckets.append(
            {
                "bucket_start": cursor.isoformat(),
                "feasible": feasible,
                "infeasible": infeasible,
                "inconclusive": inconclusive,
                "total": feasible + infeasible + inconclusive,
            }
        )
        cursor += step
    return buckets, effective_start, effective_end
