"""历史影响反查：在已持久化的审计结果里反查某个包/版本被谁依赖。

扫描每条审计结果的三个部分：

* ``resolution``：可行解中该包被选中的版本（resolution 命中）；
* ``sources``：可行解中该包收到的全部约束及其来源链，用于确定直接/传递层级；
* ``conflicts``：无解时该包作为冲突对象被约束（冲突约束命中），或作为来源链上的
  某一跳间接参与了其它包的冲突（来源链命中）。

层级（depth）定义：根插件自身为 0；来源链只含根跳（``<manifest>`` 或根插件）的约束
指向的包为**直接依赖（1）**；每多一跳传递者层级 +1。每条审计取全部命中中的最小层级。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .semver import Version, VersionError

DIRECT_DEPTH = 1  # 来源链只含根跳：清单/根插件直接声明的依赖


@dataclass
class _Match:
    """单条审计内的一处命中。"""

    match_type: str  # resolution / conflict
    role: str  # resolved / constrained / requester
    depth: int
    direct: bool
    as_root: bool
    version: Optional[str]
    conflict_kind: Optional[str]
    conflict_package: Optional[str]
    locked_version: Optional[str]
    minimal: bool
    version_range: Optional[str]
    source_chain: List[Dict[str, Any]]

    def key(self) -> tuple:
        return (
            self.match_type,
            self.role,
            self.depth,
            self.version,
            self.conflict_kind,
            self.conflict_package,
            self.locked_version,
            self.version_range,
            _chain_key(self.source_chain),
        )


@dataclass
class _Item:
    """一条被命中的审计记录的汇总。"""

    id: str
    created_at: str
    status: str
    root_plugin: Optional[str]
    min_depth: int
    versions: List[str]
    resolution_match: bool
    conflict_match: bool
    conflict_kinds: List[str]
    matches: List[_Match] = field(default_factory=list)

    @property
    def direct(self) -> bool:
        return self.min_depth == DIRECT_DEPTH

    @property
    def as_root(self) -> bool:
        return self.min_depth == 0


def _chain_key(chain: Sequence[Dict[str, Any]]) -> tuple:
    return tuple((hop.get("package"), hop.get("version"), hop.get("version_range")) for hop in chain)


def _shortest_depth(sources: Sequence[Dict[str, Any]], package: str) -> Optional[int]:
    """从 feasible 结果的 sources 中取该包最短来源链对应的层级。

    链只含根跳（<manifest> 或根插件）→ 直接依赖（1）；根插件自身没有任何约束 → 0。
    """
    best: Optional[int] = None
    for src in sources or []:
        if src.get("package") != package:
            continue
        requested = src.get("requested_by") or []
        if not requested:
            # 根插件：在 feasible 解中但没有任何来源约束
            return 0
        for constraint in requested:
            chain = constraint.get("source_chain") or []
            depth = max(len(chain), DIRECT_DEPTH)
            best = depth if best is None else min(best, depth)
    return best


def _chain_depth(chain: Sequence[Dict[str, Any]]) -> int:
    """冲突约束来源链 -> 被约束包的层级：链只含根跳为直接依赖（1）。"""
    return max(len(chain or []), DIRECT_DEPTH)


def _chain_prefix(chain: List[Dict[str, Any]], index: int) -> List[Dict[str, Any]]:
    return [dict(hop) for hop in chain[: index + 1]]


def _scan_result(
    *,
    audit_id: str,
    created_at: str,
    row_status: str,
    root_plugin: Optional[str],
    result: Dict[str, Any],
    package: str,
    version: Optional[str],
) -> Optional[_Item]:
    """扫描单条审计的 resolution/sources/conflicts，返回命中汇总；未命中返回 None。"""

    status = result.get("status") or row_status
    matches: Dict[tuple, _Match] = {}

    def add(match: _Match) -> None:
        key = match.key()
        existing = matches.get(key)
        if existing is None:
            matches[key] = match
        elif match.minimal:
            # 同一处约束既出现在 minimal_constraints 又出现在 all_constraints
            existing.minimal = True

    def version_ok(actual: Optional[str]) -> bool:
        return version is None or (actual is not None and actual == version)

    # ---------- resolution + sources（feasible 审计） ----------
    resolution = result.get("resolution") or {}
    sources = result.get("sources") or []
    if resolution and package in resolution:
        resolved_version = resolution.get(package)
        if version_ok(resolved_version):
            depth = _shortest_depth(sources, package)
            depth = DIRECT_DEPTH if depth is None else depth
            resolved_chain: List[Dict[str, Any]] = []
            src = next((s for s in sources if s.get("package") == package), None)
            if src is not None and src.get("requested_by"):
                shortest = min(
                    (c.get("source_chain") or [] for c in src["requested_by"]),
                    key=len,
                )
                resolved_chain = [dict(hop) for hop in shortest]
            add(
                _Match(
                    match_type="resolution",
                    role="resolved",
                    depth=depth,
                    direct=depth == DIRECT_DEPTH,
                    as_root=depth == 0,
                    version=resolved_version,
                    conflict_kind=None,
                    conflict_package=None,
                    locked_version=None,
                    minimal=True,
                    version_range=None,
                    source_chain=resolved_chain,
                )
            )

    # feasible 审计的 sources：沿来源链找作为“某一跳的请求方”出现的情形。
    # 被解出的包自身已由 resolution 命中覆盖；这里同时兼容历史/缺字段记录。
    for src in sources or []:
        for constraint in src.get("requested_by") or []:
            chain = constraint.get("source_chain") or []
            for index, hop in enumerate(chain):
                if hop.get("package") != package or not version_ok(hop.get("version")):
                    continue
                depth = index  # 根跳=0，其直接依赖=1，依次类推
                add(
                    _Match(
                        match_type="resolution",
                        role="requester",
                        depth=depth,
                        direct=depth == DIRECT_DEPTH,
                        as_root=depth == 0,
                        version=hop.get("version"),
                        conflict_kind=None,
                        conflict_package=None,
                        locked_version=None,
                        minimal=True,
                        version_range=hop.get("version_range"),
                        source_chain=_chain_prefix(chain, index),
                    )
                )

    # ---------- conflicts（infeasible 审计） ----------
    for conflict in result.get("conflicts") or []:
        kind = conflict.get("kind")
        conflict_package = conflict.get("package")
        locked_version = conflict.get("locked_version")

        for bucket_name, minimal_flag in (
            ("minimal_constraints", True),
            ("all_constraints", False),
        ):
            for constraint in conflict.get(bucket_name) or []:
                chain = constraint.get("source_chain") or []

                # 1) 冲突对象/约束目标就是该包：冲突约束命中
                if constraint.get("package") == package:
                    if kind == "selection_conflict":
                        actual: Optional[str] = locked_version
                    else:
                        # range_conflict：无任何版本入选；package_missing：注册表无此包。
                        # 两者都没有具体版本，指定 version 反查时不命中。
                        actual = None
                    if version_ok(actual):
                        depth = _chain_depth(chain)
                        add(
                            _Match(
                                match_type="conflict",
                                role="constrained",
                                depth=depth,
                                direct=depth == DIRECT_DEPTH,
                                as_root=False,
                                version=actual,
                                conflict_kind=kind,
                                conflict_package=conflict_package,
                                locked_version=locked_version,
                                minimal=minimal_flag,
                                version_range=constraint.get("version_range"),
                                source_chain=[dict(hop) for hop in chain],
                            )
                        )

                # 2) 该包出现在来源链上：沿链传递参与了冲突
                for index, hop in enumerate(chain):
                    if hop.get("package") != package or not version_ok(hop.get("version")):
                        continue
                    depth = index
                    add(
                        _Match(
                            match_type="conflict",
                            role="requester",
                            depth=depth,
                            direct=depth == DIRECT_DEPTH,
                            as_root=depth == 0,
                            version=hop.get("version"),
                            conflict_kind=kind,
                            conflict_package=conflict_package,
                            locked_version=locked_version,
                            minimal=minimal_flag,
                            version_range=hop.get("version_range"),
                            source_chain=_chain_prefix(chain, index),
                        )
                    )

    if not matches:
        return None

    ordered = list(matches.values())
    min_depth = min(m.depth for m in ordered)
    versions = sorted({m.version for m in ordered if m.version}, key=_version_sort_key)
    conflict_kinds = sorted({m.conflict_kind for m in ordered if m.conflict_kind})
    return _Item(
        id=audit_id,
        created_at=created_at,
        status=status,
        root_plugin=root_plugin,
        min_depth=min_depth,
        versions=versions,
        resolution_match=any(m.match_type == "resolution" for m in ordered),
        conflict_match=any(m.match_type == "conflict" for m in ordered),
        conflict_kinds=conflict_kinds,
        matches=ordered,
    )


def _version_sort_key(text: str) -> tuple:
    try:
        return (0, Version.parse(text))
    except (VersionError, ValueError):
        return (1, text)


def _match_sort_key(match: _Match) -> tuple:
    return (
        match.match_type,
        match.role,
        match.depth,
        match.conflict_package or "",
        match.conflict_kind or "",
        match.version_range or "",
        _chain_key(match.source_chain),
    )


def build_impact(
    rows: Sequence[Any],
    package: str,
    version: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """扫描已持久化审计行（sqlite3.Row，需含 result_json）。

    返回可直接序列化的影响反查响应体。未知包得到空结果（不抛错、不 404）。
    """
    items: List[_Item] = []
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except (ValueError, TypeError, KeyError):
            continue
        item = _scan_result(
            audit_id=str(row["id"]),
            created_at=str(row["created_at"]),
            row_status=str(row["status"]),
            root_plugin=row["root_plugin"],
            result=result,
            package=package,
            version=version,
        )
        if item is not None:
            items.append(item)

    # 命中层级升序（根/直接依赖在前）；同层按创建时间倒序，id 兜底保证稳定
    items.sort(key=lambda it: it.id)
    items.sort(key=lambda it: it.created_at, reverse=True)
    items.sort(key=lambda it: it.min_depth)

    summary = {
        "total": len(items),
        "feasible": sum(1 for it in items if it.status == "feasible"),
        "infeasible": sum(1 for it in items if it.status == "infeasible"),
        "inconclusive": sum(1 for it in items if it.status == "inconclusive"),
        "resolution_hits": sum(1 for it in items if it.resolution_match),
        "conflict_hits": sum(1 for it in items if it.conflict_match),
        "root": sum(1 for it in items if it.as_root),
        "direct": sum(1 for it in items if it.direct),
        "transitive": sum(1 for it in items if it.min_depth > DIRECT_DEPTH),
    }

    version_counter: Dict[str, Dict[str, int]] = {}
    for it in items:
        for ver in it.versions:
            bucket = version_counter.setdefault(
                ver, {"version": ver, "total": 0, "feasible": 0, "infeasible": 0}
            )
            bucket["total"] += 1
            if it.status in ("feasible", "infeasible"):
                bucket[it.status] += 1
    versions_summary = sorted(version_counter.values(), key=lambda v: _version_sort_key(v["version"]))

    return {
        "package": package,
        "version": version,
        "limit": limit,
        "total": len(items),
        "summary": summary,
        "versions": versions_summary,
        "items": [_serialize_item(it) for it in items[:limit]],
    }


def _serialize_item(it: _Item) -> Dict[str, Any]:
    return {
        "audit_id": it.id,
        "created_at": it.created_at,
        "status": it.status,
        "root_plugin": it.root_plugin,
        "depth": it.min_depth,
        "direct": it.direct,
        "as_root": it.as_root,
        "versions": it.versions,
        "match_types": sorted({m.match_type for m in it.matches}),
        "resolution_match": it.resolution_match,
        "conflict_match": it.conflict_match,
        "conflict_kinds": it.conflict_kinds,
        "matches": [
            {
                "match_type": m.match_type,
                "role": m.role,
                "depth": m.depth,
                "direct": m.direct,
                "as_root": m.as_root,
                "version": m.version,
                "version_range": m.version_range,
                "conflict_kind": m.conflict_kind,
                "conflict_package": m.conflict_package,
                "locked_version": m.locked_version,
                "minimal_chain": m.minimal,
                "source_chain": m.source_chain,
            }
            for m in sorted(it.matches, key=_match_sort_key)
        ],
    }
