"""传递依赖解析与版本组合搜索。

采用带 MRV（最少候选优先）启发式的回溯搜索：

* 每个被约束的包根据当前全部约束求可用版本交集；
* 选中候选版本后展开其传递依赖，把新约束加入对应包；
* 若某包候选集为空（或新约束与已锁定版本冲突），记录一条冲突并回溯；
* 遍历整棵搜索树，因此会收集**所有**本质不同的死路，而不是遇到第一个冲突即停；
* 每条死路的约束集合再做贪心极小化，得到最小冲突链与逐跳来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .semver import Version, VersionRange

# 搜索节点（尝试一个候选版本）的预算上限，防止指数爆炸
DEFAULT_NODE_BUDGET = 20_000
# 输出冲突条数上限
CONFLICT_REPORT_LIMIT = 50

MANIFEST_STEP = "<manifest>"


@dataclass(frozen=True)
class Step:
    """来源链上的一跳：某包的某版本要求了某个范围。"""

    package: str
    version_range: str
    version: Optional[str] = None  # 该跳被选中的具体版本；根跳可能为 None

    def to_dict(self) -> Dict[str, Any]:
        return {"package": self.package, "version_range": self.version_range, "version": self.version}


@dataclass(frozen=True)
class Constraint:
    package: str
    range_text: str
    rng: VersionRange
    chain: Tuple[Step, ...]  # 从根到约束提出者的路径

    def signature(self) -> Tuple[str, ...]:
        return tuple(f"{s.package}@{s.version or '*'}[{s.version_range}]" for s in self.chain) + (
            f"requires {self.package} {self.range_text}",
        )


@dataclass(frozen=True)
class PackageVersion:
    version: Version
    dependencies: Dict[str, str]  # 依赖名 -> 范围原文


@dataclass
class _Frame:
    """一次变量赋值的选择点。

    ``snapshot_*`` 是该选择点**尚未尝试任何候选时**的状态；
    回溯到该 frame 时先恢复快照，再用自增的 ``index`` 尝试下一个候选。
    """

    package: str
    candidates: List[PackageVersion]
    snapshot_constraints: Dict[str, List[Constraint]]
    snapshot_assignment: Dict[str, PackageVersion]
    index: int = 0


@dataclass
class SolveResult:
    status: str  # feasible / infeasible / inconclusive
    resolution: Optional[Dict[str, str]] = None
    sources: Optional[List[Dict[str, Any]]] = None
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    node_count: int = 0
    explanation: str = ""


class Solver:
    def __init__(
        self,
        registry: Dict[str, Dict[str, Dict[str, str]]],
        node_budget: int = DEFAULT_NODE_BUDGET,
    ) -> None:
        # registry: {包: {版本字符串: {依赖: 范围}}}；预解析为 Version 对象
        self.registry: Dict[str, List[PackageVersion]] = {}
        for name, versions in registry.items():
            pvs: List[PackageVersion] = [
                PackageVersion(Version.parse(vtext), dict(deps))
                for vtext, deps in versions.items()
            ]
            pvs.sort(key=lambda pv: pv.version, reverse=True)
            self.registry[name] = pvs
        self.node_budget = node_budget
        self._conflicts: Dict[Tuple, Dict[str, Any]] = {}

    # ---------- 公共入口 ----------

    def solve(
        self,
        root_dependencies: List[Tuple[str, str]],
        root_name: Optional[str] = None,
        root_version: Optional[str] = None,
    ) -> SolveResult:
        constraints: Dict[str, List[Constraint]] = {}
        assignment: Dict[str, PackageVersion] = {}

        if root_name is not None:
            root_step = Step(root_name, f"={root_version}", root_version)
            root_chain: Tuple[Step, ...] = (root_step,)
            root_pv = self._find_root_pv(root_name, root_version or "")
            if root_pv is not None:
                assignment[root_name] = root_pv
        else:
            root_step = Step(MANIFEST_STEP, "*", None)
            root_chain = (root_step,)

        # 清单显式依赖
        for name, range_text in root_dependencies:
            self._add_constraint(
                constraints,
                Constraint(name, range_text, VersionRange.parse(range_text), root_chain),
            )
        # 根插件在注册表里声明的依赖
        if root_name is not None and root_name in assignment:
            self._expand_dependencies(assignment[root_name], root_chain, constraints)

        stack: List[_Frame] = []
        nodes = 0

        while True:
            # ---- 观察当前分支 ----
            unwind = False

            missing = self._missing_packages(constraints)
            if missing:
                for pkg in missing:
                    self._record_missing(pkg, constraints[pkg])
                unwind = True
            else:
                unassigned = [p for p in constraints if p not in assignment]
                if not unassigned:
                    return self._feasible(assignment, constraints, nodes, root_name)

                chosen: Optional[Tuple[str, List[PackageVersion]]] = None
                for pkg in sorted(unassigned):
                    available = self._available(pkg, constraints[pkg])
                    if not available:
                        self._record_range_conflict(pkg, constraints[pkg])
                        unwind = True
                        break
                    if chosen is None or len(available) < len(chosen[1]):
                        chosen = (pkg, available)

                if not unwind:
                    assert chosen is not None
                    pkg, available = chosen
                    stack.append(
                        _Frame(
                            package=pkg,
                            candidates=available,
                            snapshot_constraints=self._copy_constraints(constraints),
                            snapshot_assignment=dict(assignment),
                        )
                    )

            # ---- 稳定到一个“活跃选择”：让栈顶 frame 成功选中一个候选 ----
            while True:
                if unwind:
                    # 弹出观察阶段发现死路的那个选择点
                    if stack:
                        stack.pop()
                    unwind = False

                if not stack:
                    return self._infeasible(nodes)

                top = stack[-1]
                # 一律恢复该选择点“试候选之前”的状态，再尝试下一个候选；
                # 这样子树穷尽后回到父 frame 时会自然推进到父的下一候选
                constraints = self._copy_constraints(top.snapshot_constraints)
                assignment = dict(top.snapshot_assignment)

                if self._advance(top, constraints, assignment):
                    nodes += 1
                    if nodes > self.node_budget:
                        return self._inconclusive(nodes)
                    break
                # 该 frame 候选全部被否：弹出，回到循环上层处理父选择点
                stack.pop()

    # ---------- 状态操作 ----------

    @staticmethod
    def _copy_constraints(
        constraints: Dict[str, List[Constraint]],
    ) -> Dict[str, List[Constraint]]:
        # Constraint/Step 均为 frozen，只需复制容器即可安全隔离后续追加
        return {pkg: list(bucket) for pkg, bucket in constraints.items()}

    def _find_root_pv(self, name: str, version_text: str) -> Optional[PackageVersion]:
        for pv in self.registry.get(name, []):
            if str(pv.version) == version_text:
                return pv
        return None

    @staticmethod
    def _add_constraint(constraints: Dict[str, List[Constraint]], c: Constraint) -> bool:
        bucket = constraints.setdefault(c.package, [])
        for existing in bucket:
            if existing.range_text == c.range_text and existing.signature() == c.signature():
                return False
        bucket.append(c)
        return True

    @staticmethod
    def _shortest_chain(
        constraints: Dict[str, List[Constraint]], pkg: str
    ) -> Tuple[Step, ...]:
        return min(
            (c.chain for c in constraints.get(pkg, [])),
            key=lambda ch: (len(ch),) + tuple(s.package for s in ch),
        )

    def _expand_dependencies(
        self,
        pv: PackageVersion,
        chain: Tuple[Step, ...],
        constraints: Dict[str, List[Constraint]],
    ) -> None:
        for dep_name, range_text in pv.dependencies.items():
            self._add_constraint(
                constraints,
                Constraint(dep_name, range_text, VersionRange.parse(range_text), chain),
            )

    def _advance(
        self,
        frame: _Frame,
        constraints: Dict[str, List[Constraint]],
        assignment: Dict[str, PackageVersion],
    ) -> bool:
        """尝试 frame 的下一个候选。True=展开成功；False=候选已全部被否。"""
        while frame.index < len(frame.candidates):
            candidate = frame.candidates[frame.index]
            frame.index += 1

            base_chain = self._shortest_chain(constraints, frame.package)
            chosen_step = Step(frame.package, f"={candidate.version}", str(candidate.version))
            new_chain = base_chain + (chosen_step,)

            new_constraints: List[Constraint] = []
            lock_conflicts: List[Tuple[Constraint, str]] = []
            for dep_name, range_text in candidate.dependencies.items():
                c = Constraint(dep_name, range_text, VersionRange.parse(range_text), new_chain)
                new_constraints.append(c)
                if dep_name == frame.package:
                    # 自依赖：直接用候选版本自身比对
                    if not c.rng.satisfies(candidate.version):
                        lock_conflicts.append((c, str(candidate.version)))
                elif dep_name in assignment and not c.rng.satisfies(assignment[dep_name].version):
                    lock_conflicts.append((c, str(assignment[dep_name].version)))

            if lock_conflicts:
                # 该候选不可取：以死路形态记录（锁死约束 + 拒绝约束一起极小化）
                for c, locked in lock_conflicts:
                    self._record_dead_end(
                        c.package,
                        constraints.get(c.package, []),
                        reject=c,
                        locked_version=locked,
                        requirer=frame.package,
                        requirer_version=str(candidate.version),
                    )
                continue  # 试同 frame 的下一个版本（状态未被污染）

            assignment[frame.package] = candidate
            for c in new_constraints:
                self._add_constraint(constraints, c)
            return True
        return False

    def _available(self, pkg: str, cs: List[Constraint]) -> List[PackageVersion]:
        return [
            pv
            for pv in self.registry.get(pkg, [])
            if all(c.rng.satisfies(pv.version) for c in cs)
        ]

    def _missing_packages(self, constraints: Dict[str, List[Constraint]]) -> List[str]:
        return sorted(pkg for pkg in constraints if pkg not in self.registry)

    # ---------- 冲突记录与极小化 ----------

    def _constraint_dicts(self, cs: List[Constraint]) -> List[Dict[str, Any]]:
        return [
            {
                "package": c.package,
                "version_range": c.range_text,
                "source_chain": [step.to_dict() for step in c.chain],
            }
            for c in cs
        ]

    @staticmethod
    def _order_key(c: Constraint) -> Tuple:
        return (len(c.chain), c.range_text, c.signature())

    def _minimal_range_subset(self, pkg: str, cs: List[Constraint]) -> List[Constraint]:
        """贪心求仍为空交集的最小约束子集。"""
        ordered = sorted(cs, key=self._order_key)
        minimal = list(ordered)
        for c in ordered:
            trial = [x for x in minimal if x is not c]
            if trial and not self._available(pkg, trial):
                minimal = trial
        return sorted(minimal, key=self._order_key)

    def _record_range_conflict(self, pkg: str, cs: List[Constraint]) -> None:
        minimal = self._minimal_range_subset(pkg, cs)
        key = (
            "range_conflict",
            pkg,
            None,
            frozenset((c.range_text, c.signature()) for c in minimal),
        )
        if key in self._conflicts:
            return
        chain_desc = " -> ".join(minimal[0].signature())
        self._conflicts[key] = {
            "package": pkg,
            "kind": "range_conflict",
            "locked_version": None,
            "message": (
                f"包 {pkg} 不存在能同时满足 {len(minimal)} 条约束的版本；"
                f"最小冲突链：{chain_desc}"
            ),
            "minimal_constraints": self._constraint_dicts(minimal),
            "all_constraints": self._constraint_dicts(sorted(cs, key=self._order_key)),
        }

    def _minimal_locked_subset(
        self,
        locked: Version,
        old: List[Constraint],
        reject: Constraint,
    ) -> List[Constraint]:
        """求使 locked 仍被拒绝的最小约束集（必含 reject）。"""
        ordered = sorted(old, key=self._order_key)
        minimal = list(ordered)
        for c in ordered:
            trial = [x for x in minimal if x is not c]
            # 去掉 c 后，剩余旧约束不再把选择锁在 locked 上（locked 不再全满足），
            # 说明 c 是“锁定来源”的关键约束，不能删；否则 c 冗余，可删。
            if all(x.rng.satisfies(locked) for x in trial):
                continue
            minimal = trial
        return sorted(minimal + [reject], key=self._order_key)

    def _record_dead_end(
        self,
        pkg: str,
        old_constraints: List[Constraint],
        reject: Constraint,
        locked_version: str,
        requirer: str,
        requirer_version: str,
    ) -> None:
        """锁定版本冲突死路：对“旧约束（锁定来源）+ 拒绝约束”做极小化。"""
        locked = Version.parse(locked_version)
        minimal = self._minimal_locked_subset(locked, old_constraints, reject)
        key = (
            "selection_conflict",
            pkg,
            locked_version,
            frozenset((c.range_text, c.signature()) for c in minimal),
        )
        if key in self._conflicts:
            return
        all_on_pkg = sorted(old_constraints + [reject], key=self._order_key)
        chain_desc = " -> ".join(reject.signature())
        self._conflicts[key] = {
            "package": pkg,
            "kind": "selection_conflict",
            "locked_version": locked_version,
            "message": (
                f"{pkg} 已被锁定为 {locked_version}，但该版本不满足来自 "
                f"{requirer}@{requirer_version} 的约束（{chain_desc}）；"
                f"{len(minimal)} 条约束构成最小冲突链"
            ),
            "minimal_constraints": self._constraint_dicts(minimal),
            "all_constraints": self._constraint_dicts(all_on_pkg),
        }

    def _record_missing(self, pkg: str, cs: List[Constraint]) -> None:
        ordered = sorted(cs, key=self._order_key)
        key = (
            "package_missing",
            pkg,
            (ordered[0].range_text, ordered[0].signature()) if ordered else None,
        )
        if key in self._conflicts:
            return
        self._conflicts[key] = {
            "package": pkg,
            "kind": "package_missing",
            "locked_version": None,
            "message": f"注册表中不存在包 {pkg}（被 {ordered[0].range_text if ordered else '?'} 要求）",
            "minimal_constraints": self._constraint_dicts(ordered[:1]),
            "all_constraints": self._constraint_dicts(ordered),
        }

    # ---------- 结果组装 ----------

    def _sorted_conflicts(self) -> List[Dict[str, Any]]:
        conflicts = list(self._conflicts.values())

        def sort_key(item: Dict[str, Any]) -> Tuple[str, str, int, str]:
            first = item["minimal_constraints"][0] if item["minimal_constraints"] else None
            chain_len = len(first["source_chain"]) if first else 999
            return (item["package"], item["kind"], chain_len, item["message"])

        conflicts.sort(key=sort_key)
        return conflicts[:CONFLICT_REPORT_LIMIT]

    def _feasible(
        self,
        assignment: Dict[str, PackageVersion],
        constraints: Dict[str, List[Constraint]],
        nodes: int,
        root_name: Optional[str],
    ) -> SolveResult:
        resolution = {pkg: str(pv.version) for pkg, pv in assignment.items()}
        sources: List[Dict[str, Any]] = []
        for pkg in sorted(resolution):
            cs = constraints.get(pkg, [])
            sources.append(
                {
                    "package": pkg,
                    "version": resolution[pkg],
                    "requested_by": self._constraint_dicts(sorted(cs, key=self._order_key)),
                }
            )
        return SolveResult(
            status="feasible",
            resolution=resolution,
            sources=sources,
            conflicts=[],
            node_count=nodes,
            explanation=(
                f"找到可行版本组合，共 {len(resolution)} 个包（含根插件）"
                if root_name
                else f"找到可行版本组合，共 {len(resolution)} 个包"
            ),
        )

    def _infeasible(self, nodes: int) -> SolveResult:
        conflicts = self._sorted_conflicts()
        return SolveResult(
            status="infeasible",
            resolution=None,
            sources=None,
            conflicts=conflicts,
            node_count=nodes,
            explanation=(
                f"搜索树已穷尽：不存在可行版本组合；共发现 {len(conflicts)} 条本质不同的最小冲突链"
                if conflicts
                else "搜索树已穷尽：不存在可行版本组合"
            ),
        )

    def _inconclusive(self, nodes: int) -> SolveResult:
        return SolveResult(
            status="inconclusive",
            resolution=None,
            sources=None,
            conflicts=[],
            node_count=nodes,
            explanation=f"搜索节点超过预算 {self.node_budget}，未能在预算内判定可行性",
        )
