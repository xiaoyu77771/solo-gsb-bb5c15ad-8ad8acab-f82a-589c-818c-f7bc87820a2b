"""HTTP 请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .semver import Version, VersionError, VersionRange


class DependencySpec(BaseModel):
    """清单中声明的一条依赖。"""

    name: str = Field(..., min_length=1, description="依赖包名")
    range: str = Field(..., min_length=1, description="SemVer 版本范围，如 ^1.2.0、>=1.0.0 <2.0.0")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("依赖名不能为空")
        if any(ch.isspace() for ch in value):
            raise ValueError("依赖名不能包含空白字符")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        try:
            VersionRange.parse(value)
        except VersionError as exc:
            raise ValueError(f"非法版本范围 {value!r}: {exc}") from exc
        return value


class ManifestIn(BaseModel):
    """客户端提交的插件清单。"""

    plugin_name: Optional[str] = Field(
        None, description="清单自身（根插件）名称；填写后会一并解析其注册表依赖"
    )
    plugin_version: Optional[str] = Field(None, description="根插件版本，需为合法 SemVer")
    dependencies: List[DependencySpec] = Field(default_factory=list)

    @field_validator("plugin_version")
    @classmethod
    def _validate_plugin_version(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            return str(Version.parse(value))
        except VersionError as exc:
            raise ValueError(f"非法插件版本 {value!r}: {exc}") from exc

    def validate_root_consistency(self) -> None:
        if (self.plugin_name is None) != (self.plugin_version is None):
            raise ValueError("plugin_name 与 plugin_version 必须同时提供或同时省略")


class PackageVersionIn(BaseModel):
    """注册表写入：单个版本。"""

    version: str
    dependencies: Dict[str, str] = Field(
        default_factory=dict, description="包名 -> SemVer 范围"
    )

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            return str(Version.parse(value))
        except VersionError as exc:
            raise ValueError(f"非法版本 {value!r}: {exc}") from exc

    @field_validator("dependencies")
    @classmethod
    def _validate_dependencies(cls, value: Dict[str, str]) -> Dict[str, str]:
        for name, spec in value.items():
            if not name.strip() or any(ch.isspace() for ch in name):
                raise ValueError(f"非法依赖名: {name!r}")
            try:
                VersionRange.parse(spec)
            except VersionError as exc:
                raise ValueError(f"依赖 {name} 的范围非法 {spec!r}: {exc}") from exc
        return value


class PackageIn(BaseModel):
    """注册表写入：整包。"""

    name: str = Field(..., min_length=1)
    versions: List[PackageVersionIn] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("包名非法")
        return value


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    packages: int


class AuditSummary(BaseModel):
    id: str
    created_at: str
    status: Literal["feasible", "infeasible", "inconclusive"]
    root_plugin: Optional[str]
    dependency_count: int
    conflict_count: int


# ---------- 审计结果时间桶汇总 ----------


class AuditSummaryBucketOut(BaseModel):
    """单个时间桶的状态计数（空桶同样补零返回）。"""

    bucket_start: str = Field(..., description="桶起始时刻（UTC ISO 8601）；day=UTC 零点，week=周一 UTC 零点")
    feasible: int
    infeasible: int
    inconclusive: int
    total: int = Field(..., description="桶内三种状态记录总数")


class AuditSummaryResponse(BaseModel):
    """GET /api/v1/audits/summary：按 day/week 桶汇总已保存审计结果。"""

    bucket: Literal["day", "week"]
    start: Optional[str] = Field(
        None, description="统计区间起始（UTC ISO 8601，闭区间）；无记录且未指定时为 null"
    )
    end: Optional[str] = Field(
        None, description="统计区间结束（UTC ISO 8601，开区间）；无记录且未指定时为 null"
    )
    total: int = Field(..., description="区间内全部桶的审计记录总数")
    buckets: List[AuditSummaryBucketOut] = Field(
        ..., description="按桶起始时刻升序，空桶补零"
    )


class ConflictStepOut(BaseModel):
    package: str
    version_range: str
    version: Optional[str] = None


class ConstraintOut(BaseModel):
    package: str
    version_range: str
    source_chain: List[ConflictStepOut]


class ConflictOut(BaseModel):
    package: str
    kind: Literal["range_conflict", "selection_conflict", "package_missing"]
    locked_version: Optional[str] = None
    message: str
    minimal_constraints: List[ConstraintOut]
    all_constraints: List[ConstraintOut]


class ResolutionSourceOut(BaseModel):
    package: str
    version: str
    requested_by: List[ConflictStepOut]


class AuditResponse(BaseModel):
    id: str
    created_at: str
    status: Literal["feasible", "infeasible", "inconclusive"]
    root_plugin: Optional[str]
    root_version: Optional[str]
    explanation: str
    node_count: int
    resolution: Optional[Dict[str, str]]
    sources: Optional[List[ResolutionSourceOut]]
    conflicts: List[ConflictOut]


# ---------- 批量审计 ----------


class BatchAuditIn(BaseModel):
    """批量审计请求：1～20 个待审计清单（原始字典，逐项独立校验）。"""

    manifests: List[Any] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="按输入顺序逐个审计的清单列表，数量 1～20；元素格式与单条 POST /api/v1/audits 请求体一致",
    )


class BatchAuditError(BaseModel):
    """单项审计失败时的结构化错误。"""

    code: Literal["invalid_manifest", "solver_error", "internal_error"] = Field(
        ...,
        description=(
            "invalid_manifest=清单校验失败（非法 SemVer/范围、根插件不存在等 422 类问题）；"
            "solver_error=求解过程异常；internal_error=持久化等服务端异常"
        ),
    )
    message: str
    details: Optional[List[Dict[str, Any]]] = Field(
        None, description="结构化校验细节（Pydantic 错误列表），仅 invalid_manifest 可能携带"
    )


class BatchAuditItemOut(BaseModel):
    """批量结果中的单项：成功携带 audit_id/status/审计结果，失败携带 error。"""

    index: int = Field(..., description="该项在请求 manifests 中的下标（0 起）")
    audit_id: Optional[str] = Field(None, description="审计记录 ID；失败时为 null")
    status: Optional[Literal["feasible", "infeasible", "inconclusive"]] = Field(
        None, description="求解状态；失败时为 null"
    )
    audit: Optional[AuditResponse] = Field(
        None, description="完整审计结果（与 GET /api/v1/audits/{id} 同构）；失败时为 null"
    )
    error: Optional[BatchAuditError] = Field(None, description="结构化错误；成功时为 null")


class BatchAuditResponse(BaseModel):
    total: int = Field(..., description="提交的清单总数（1～20）")
    feasible: int = Field(..., description="可行（feasible）项数")
    infeasible: int = Field(..., description="无解（infeasible）项数")
    inconclusive: int = Field(..., description="预算内无法判定（inconclusive）项数")
    error: int = Field(..., description="校验失败或求解异常的项数")
    items: List[BatchAuditItemOut] = Field(
        ..., description="逐项结果，顺序与请求 manifests 完全一致"
    )


# ---------- 历史影响反查 ----------


# ---------- 异步审计作业 ----------


class AuditJobIn(BaseModel):
    """异步作业请求：单条清单（manifest）或批量清单（manifests，1～20 条）二选一。

    清单内容不在提交时校验：非法清单在后台逐项求解时记为该项的 invalid_manifest
    错误，与同步批量接口的逐项隔离语义一致。
    """

    manifest: Optional[Any] = Field(
        None, description="单条清单，格式与 POST /api/v1/audits 请求体一致"
    )
    manifests: Optional[List[Any]] = Field(
        None,
        max_length=20,
        description="批量清单（1～20 条），元素格式与 POST /api/v1/audits 请求体一致",
    )

    @model_validator(mode="after")
    def _check_exactly_one(self) -> "AuditJobIn":
        if self.manifest is None and self.manifests is None:
            raise ValueError("必须提供 manifest（单条）或 manifests（批量）之一")
        if self.manifest is not None and self.manifests is not None:
            raise ValueError("manifest 与 manifests 只能二选一，不能同时提供")
        if self.manifests is not None and len(self.manifests) == 0:
            raise ValueError("manifests 至少需要 1 条清单")
        return self

    def raw_manifests(self) -> List[Any]:
        """返回统一形态的原始清单列表（保留原始类型，交由 worker 逐项校验）。"""
        return [self.manifest] if self.manifest is not None else list(self.manifests or [])


class AuditJobItemError(BaseModel):
    """异步作业单项失败时的结构化错误（与同步批量错误码一致）。"""

    code: Literal["invalid_manifest", "solver_error", "internal_error"]
    message: str
    details: Optional[List[Dict[str, Any]]] = None


class AuditJobItemOut(BaseModel):
    """作业中的逐项状态与结果。"""

    index: int = Field(..., description="该项在提交清单中的下标（0 起）")
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    audit_id: Optional[str] = Field(None, description="成功落库的审计记录 ID")
    audit: Optional[AuditResponse] = Field(
        None, description="完整审计结果（与 GET /api/v1/audits/{id} 同构）；仅成功时存在"
    )
    error: Optional[AuditJobItemError] = Field(None, description="结构化错误；仅失败时存在")


class AuditJobCreatedResponse(BaseModel):
    """POST /api/v1/audit-jobs 的即时响应。"""

    job_id: str
    status: Literal["queued", "running"]
    total: int = Field(..., description="作业包含的条目总数（1～20）")
    created_at: str
    items: List[AuditJobItemOut] = Field(
        ..., description="提交时全部条目均为 queued 的逐项视图"
    )


class AuditJobCounts(BaseModel):
    total: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int = Field(..., description="被 DELETE 取消的条目数")


class AuditJobResponse(BaseModel):
    """GET /api/v1/audit-jobs/{job_id} 的作业详情。"""

    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    total: int
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    counts: AuditJobCounts
    items: List[AuditJobItemOut]


# ---------- 历史影响反查 ----------


class ImpactMatchOut(BaseModel):
    """单条审计内的一处命中（resolution 命中或冲突约束命中）。"""

    match_type: Literal["resolution", "conflict"] = Field(
        ..., description="resolution=可行解命中；conflict=冲突约束/来源链命中"
    )
    role: Literal["resolved", "constrained", "requester"] = Field(
        ...,
        description=(
            "resolved=可行解中被选中；constrained=作为冲突对象被约束；"
            "requester=仅出现在来源链上（传递参与）"
        ),
    )
    depth: int = Field(..., description="命中层级：0=根插件自身，1=直接依赖，>=2=传递依赖")
    direct: bool
    as_root: bool
    version: Optional[str] = Field(None, description="命中的具体版本（range 冲突无版本时为 null）")
    version_range: Optional[str] = Field(None, description="该跳/该约束声明的版本范围")
    conflict_kind: Optional[
        Literal["range_conflict", "selection_conflict", "package_missing"]
    ] = None
    conflict_package: Optional[str] = Field(None, description="冲突所属的包")
    locked_version: Optional[str] = None
    minimal_chain: bool = Field(
        ..., description="该约束是否出现在最小冲突链中（resolution 命中恒为 true）"
    )
    source_chain: List[ConflictStepOut] = Field(
        ..., description="从根跳到命中位置的逐跳来源链"
    )


class ImpactItemOut(BaseModel):
    audit_id: str
    created_at: str
    status: Literal["feasible", "infeasible", "inconclusive"]
    root_plugin: Optional[str]
    depth: int = Field(..., description="该审计内全部命中的最小层级")
    direct: bool = Field(..., description="最小层级是否为直接依赖（depth=1）")
    as_root: bool
    versions: List[str] = Field(..., description="该审计内命中的全部具体版本")
    match_types: List[Literal["resolution", "conflict"]]
    resolution_match: bool
    conflict_match: bool
    conflict_kinds: List[
        Literal["range_conflict", "selection_conflict", "package_missing"]
    ]
    matches: List[ImpactMatchOut]


class ImpactVersionCountOut(BaseModel):
    version: str
    total: int
    feasible: int
    infeasible: int


class ImpactSummaryOut(BaseModel):
    total: int
    feasible: int
    infeasible: int
    inconclusive: int
    resolution_hits: int
    conflict_hits: int
    root: int
    direct: int
    transitive: int


class ImpactResponse(BaseModel):
    package: str
    version: Optional[str]
    limit: int
    total: int = Field(..., description="命中审计总数（limit 之前）")
    summary: ImpactSummaryOut
    versions: List[ImpactVersionCountOut] = Field(
        ..., description="按版本聚合的命中计数（未指定 version 时覆盖全部版本）"
    )
    items: List[ImpactItemOut]


# ---------- 可验证导出包 ----------

class BundleExportIn(BaseModel):
    """导出包请求：按时间排序的审计 ID 列表（1～100）。"""

    audit_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="按创建时间排序的审计 ID 列表（1～100 个，不可重复）",
    )
    include_conflicts: bool = Field(
        False, description="导出的每条记录是否包含 conflicts 字段"
    )

    @field_validator("audit_ids")
    @classmethod
    def _validate_audit_ids(cls, value: List[str]) -> List[str]:
        for audit_id in value:
            if not isinstance(audit_id, str) or not audit_id.strip():
                raise ValueError("audit_id 不能为空")
        return value


class BundleRecordDigest(BaseModel):
    audit_id: str
    sha256: str


class BundleManifest(BaseModel):
    format: str
    format_version: str
    include_conflicts: bool
    record_count: int
    payload_sha256: str
    records: List[BundleRecordDigest]
    manifest_sha256: str


class BundleExportResponse(BaseModel):
    format: str
    format_version: str
    include_conflicts: bool
    record_count: int
    jsonl: str = Field(..., description="确定性 JSONL 文本，每行一条审计记录并以 \\n 结尾")
    records: List[BundleRecordDigest] = Field(..., description="每条记录的 sha256（对该行字节）")
    manifest: BundleManifest
    manifest_sha256: str = Field(..., description="清单自身（除该字段外）的 sha256")
    gzip_base64: str = Field(..., description="信封 JSON（manifest+jsonl）经 gzip 后的 Base64")


class BundleVerifyIn(BaseModel):
    """校验请求：二选一——直接传包的 Base64，或分别传 jsonl/manifest。"""

    gzip_base64: Optional[str] = Field(None, description="导出接口返回的 gzip Base64 包")
    jsonl: Optional[str] = Field(None, description="与 manifest 一起提交的 JSONL 文本")
    manifest: Optional[Dict[str, object]] = Field(None, description="与 jsonl 一起提交的清单")


class BundleVerifyResponse(BaseModel):
    verified: bool
    first_error_index: int = Field(
        ..., description="首个出错记录下标（0 起）；错误不针对记录时为 -1"
    )
    error_type: Optional[
        Literal[
            "bundle_base64_invalid",
            "bundle_gzip_invalid",
            "bundle_envelope_invalid",
            "malformed_manifest",
            "manifest_digest_mismatch",
            "unsupported_format",
            "malformed_jsonl",
            "record_count_mismatch",
            "record_parse_error",
            "record_id_missing",
            "record_order_mismatch",
            "record_digest_mismatch",
            "conflicts_flag_mismatch",
            "payload_digest_mismatch",
        ]
    ] = None
    error_detail: Optional[str] = None
