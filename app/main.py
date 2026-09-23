"""FastAPI 入口：插件依赖兼容性审计 API。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .bootstrap import initialize_database
from .bundle import build_bundle, decode_bundle_base64, verify_bundle
from .database import (
    DEFAULT_DB_PATH,
    cancel_job_item,
    connect,
    count_audits,
    count_job_items,
    finalize_job_if_done,
    get_audit,
    get_audit_job,
    get_audit_job_item,
    insert_audit,
    insert_audit_job,
    iter_audits,
    list_audit_created_status,
    list_audit_job_items,
    list_audits,
    load_registry,
    package_count,
    upsert_package,
    utcnow_iso,
)
from .impact import build_impact
from .jobs import JobRunner
from .models import (
    AuditJobCounts,
    AuditJobCreatedResponse,
    AuditJobIn,
    AuditJobItemError,
    AuditJobItemOut,
    AuditJobResponse,
    AuditResponse,
    AuditSummary,
    AuditSummaryResponse,
    BatchAuditError,
    BatchAuditIn,
    BatchAuditItemOut,
    BatchAuditResponse,
    BundleExportIn,
    BundleExportResponse,
    BundleVerifyIn,
    BundleVerifyResponse,
    HealthResponse,
    ImpactResponse,
    ManifestIn,
    PackageIn,
)
from .semver import Version, VersionError
from .solver import Solver
from .summary import InvalidTimeBound, build_audit_summary, parse_utc_bound

logger = logging.getLogger(__name__)

SEED_PATH = os.environ.get("AUDIT_SEED_PATH", "/app/data/seed_registry.json")
MAX_LIMIT = 100
BATCH_MAX_ITEMS = 20
# 测试可通过该变量缩短停机等待（等待正在处理的条目结束）；生产默认 10s
JOB_STOP_TIMEOUT = float(os.environ.get("AUDIT_JOB_STOP_TIMEOUT", "10"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 建表 + 首次启动播种（幂等；容器场景也可由 entrypoint 提前执行）
    initialize_database(seed_path=SEED_PATH, db_path=DEFAULT_DB_PATH)
    # 常驻连接（WAL 模式，FastAPI 同步路由）
    app.state.conn = connect(DEFAULT_DB_PATH)
    # 重启恢复未完成作业，随后启动后台 worker
    runner = JobRunner(db_path=DEFAULT_DB_PATH, stop_timeout=JOB_STOP_TIMEOUT)
    runner.recover()
    runner.start()
    app.state.job_runner = runner
    try:
        yield
    finally:
        runner.stop()
        app.state.conn.close()


app = FastAPI(
    title="插件依赖兼容性审计 API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "提交插件清单（依赖名 + SemVer 范围），服务解析传递依赖、搜索可行版本组合；"
        "无解时返回全部最小冲突链及每条约束的来源链。"
    ),
)


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn  # type: ignore[no-any-return]


@app.exception_handler(VersionError)
async def _version_error_handler(_request: Request, exc: VersionError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", tags=["meta"])
def index() -> dict:
    return {
        "service": "plugin-dependency-audit",
        "version": __version__,
        "docs": "/docs",
        "endpoints": [
            "GET /health",
            "POST /api/v1/audits",
            "POST /api/v1/audits/batch",
            "GET /api/v1/audits",
            "GET /api/v1/audits/summary",
            "GET /api/v1/audits/{audit_id}",
            "GET /api/v1/audits/impact/{package_name}",
            "POST /api/v1/audits/export-bundle",
            "POST /api/v1/audits/verify-bundle",
            "POST /api/v1/audit-jobs",
            "GET /api/v1/audit-jobs/{job_id}",
            "DELETE /api/v1/audit-jobs/{job_id}/items/{item_index}",
            "PUT /api/v1/registry/packages/{name}",
            "GET /api/v1/registry/packages",
        ],
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(request: Request) -> HealthResponse:
    db_ok = True
    packages = 0
    try:
        conn = get_conn(request)
        conn.execute("SELECT 1").fetchone()
        packages = package_count(conn)
    except Exception:
        db_ok = False
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        database="ok" if db_ok else "error",
        packages=packages,
    )


class _RootPluginNotFound(Exception):
    """根插件版本不存在于注册表（单条接口映射为 422，批量接口映射为单项错误）。"""


def _root_label(manifest: ManifestIn) -> Optional[str]:
    if not manifest.plugin_name:
        return None
    if manifest.plugin_version:
        return f"{manifest.plugin_name}@{manifest.plugin_version}"
    return manifest.plugin_name


def _solve_manifest(
    manifest: ManifestIn, registry: dict
) -> tuple[Any, Optional[str], Optional[str]]:
    """校验清单并运行求解器，返回 (result, root_label, root_version)。

    仅做内存中的校验与求解，不触碰数据库。
    """
    manifest.validate_root_consistency()

    # 根插件版本必须存在于注册表（若声明了根插件）
    if (
        manifest.plugin_name is not None
        and manifest.plugin_version not in registry.get(manifest.plugin_name, {})
    ):
        raise _RootPluginNotFound(
            f"根插件 {manifest.plugin_name}@{manifest.plugin_version} 不存在于注册表；"
            f"可用版本：{sorted(registry.get(manifest.plugin_name, {}), reverse=True)}"
        )

    solver = Solver(registry)
    result = solver.solve(
        root_dependencies=[(d.name, d.range) for d in manifest.dependencies],
        root_name=manifest.plugin_name,
        root_version=manifest.plugin_version,
    )
    return result, _root_label(manifest), manifest.plugin_version


def _build_audit_body(
    manifest: ManifestIn, result: Any, root_label: Optional[str], root_version: Optional[str]
) -> tuple[str, str, dict]:
    """组装待持久化/返回的审计体，返回 (audit_id, created_at, body)。"""
    audit_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    body = {
        "id": audit_id,
        "created_at": created_at,
        "status": result.status,
        "root_plugin": root_label,
        "root_version": root_version,
        "explanation": result.explanation,
        "node_count": result.node_count,
        "resolution": result.resolution,
        "sources": result.sources,
        "conflicts": result.conflicts,
    }
    return audit_id, created_at, body


def _persist_audit(
    conn: sqlite3.Connection,
    manifest: ManifestIn,
    result: Any,
    audit_id: str,
    created_at: str,
    body: dict,
    root_label: Optional[str],
    root_version: Optional[str],
) -> None:
    insert_audit(
        conn,
        audit_id=audit_id,
        created_at=created_at,
        status=result.status,
        root_plugin=root_label,
        root_version=root_version,
        dependency_count=len(manifest.dependencies),
        manifest=manifest.model_dump(mode="json"),
        result=body,
        node_count=result.node_count,
    )


@app.post(
    "/api/v1/audits",
    response_model=AuditResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["audits"],
)
def create_audit(manifest: ManifestIn, request: Request) -> AuditResponse:
    conn = get_conn(request)
    registry = load_registry(conn)

    try:
        result, root_label, root_version = _solve_manifest(manifest, registry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _RootPluginNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    audit_id, created_at, body = _build_audit_body(manifest, result, root_label, root_version)
    _persist_audit(
        conn, manifest, result, audit_id, created_at, body, root_label, root_version
    )
    return AuditResponse(**body)


@app.post(
    "/api/v1/audits/batch",
    response_model=BatchAuditResponse,
    status_code=status.HTTP_200_OK,
    tags=["audits"],
    summary="批量审计：1～20 个清单按输入顺序独立求解并逐项持久化",
)
def create_audits_batch(payload: BatchAuditIn, request: Request) -> BatchAuditResponse:
    """逐项独立运行求解器并落库，单项失败不影响其他项。

    * 每个清单项单独做 Pydantic 校验与根插件检查，失败仅记为该项的结构化 error；
    * 求解器或持久化抛出的异常同样隔离在该项内；
    * 成功项与单条 ``POST /api/v1/audits`` 行为完全一致（独立 audit_id、独立落库）；
    * ``items`` 顺序与请求 ``manifests`` 一致，计数覆盖全部输入项。
    """
    conn = get_conn(request)
    # 注册表在本批次开始时加载一次，供全部项共享（各项之间不产生新注册表数据）
    registry = load_registry(conn)

    items: list[BatchAuditItemOut] = []
    counts = {"feasible": 0, "infeasible": 0, "inconclusive": 0, "error": 0}

    for index, raw in enumerate(payload.manifests):
        # ---- 阶段一：逐项校验 + 求解（纯内存） ----
        try:
            manifest = ManifestIn.model_validate(raw)
            result, root_label, root_version = _solve_manifest(manifest, registry)
            audit_id, created_at, body = _build_audit_body(
                manifest, result, root_label, root_version
            )
        except ValidationError as exc:
            counts["error"] += 1
            items.append(
                BatchAuditItemOut(
                    index=index,
                    error=BatchAuditError(
                        code="invalid_manifest",
                        message="清单校验失败",
                        details=jsonable_encoder(exc.errors()),
                    ),
                )
            )
            continue
        except (_RootPluginNotFound, ValueError) as exc:
            counts["error"] += 1
            items.append(
                BatchAuditItemOut(
                    index=index,
                    error=BatchAuditError(code="invalid_manifest", message=str(exc)),
                )
            )
            continue
        except Exception:  # noqa: BLE001 - 单项求解异常不得影响其他项
            logger.exception("批量审计第 %d 项求解失败", index)
            counts["error"] += 1
            items.append(
                BatchAuditItemOut(
                    index=index,
                    error=BatchAuditError(code="solver_error", message="求解过程中发生内部错误"),
                )
            )
            continue

        # ---- 阶段二：逐项持久化（独立事务） ----
        try:
            _persist_audit(
                conn, manifest, result, audit_id, created_at, body, root_label, root_version
            )
        except Exception:  # noqa: BLE001 - 单项落库异常不得影响其他项
            logger.exception("批量审计第 %d 项持久化失败", index)
            counts["error"] += 1
            items.append(
                BatchAuditItemOut(
                    index=index,
                    error=BatchAuditError(code="internal_error", message="审计结果持久化失败"),
                )
            )
            continue

        counts[result.status] += 1
        items.append(
            BatchAuditItemOut(
                index=index,
                audit_id=audit_id,
                status=result.status,
                audit=AuditResponse(**body),
            )
        )

    return BatchAuditResponse(
        total=len(payload.manifests),
        feasible=counts["feasible"],
        infeasible=counts["infeasible"],
        inconclusive=counts["inconclusive"],
        error=counts["error"],
        items=items,
    )


# ---------- 异步审计作业 ----------


def _job_item_out(conn: sqlite3.Connection, row: sqlite3.Row) -> AuditJobItemOut:
    """把一行 audit_job_items 组装成对外的逐项视图。"""
    audit = None
    if row["status"] == "succeeded":
        result_json = row["result_json"]
        if result_json:
            audit = AuditResponse(**json.loads(result_json))
        elif row["audit_id"]:
            # 兼容直接从审计表取回（正常路径下 result_json 已内联）
            record = get_audit(conn, row["audit_id"])
            if record is not None:
                audit = AuditResponse(**json.loads(record["result_json"]))
    error = None
    if row["status"] == "failed":
        details = None
        if row["error_details_json"]:
            details = json.loads(row["error_details_json"])
        error = AuditJobItemError(
            code=row["error_code"], message=row["error_message"], details=details
        )
    return AuditJobItemOut(
        index=row["item_index"],
        status=row["status"],
        audit_id=row["audit_id"],
        audit=audit,
        error=error,
    )


def _build_job_response(conn: sqlite3.Connection, job: sqlite3.Row) -> AuditJobResponse:
    item_rows = list_audit_job_items(conn, job["id"])
    items = [_job_item_out(conn, row) for row in item_rows]
    counts = count_job_items(conn, job["id"])
    return AuditJobResponse(
        job_id=job["id"],
        status=job["status"],
        total=job["total"],
        created_at=job["created_at"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        counts=AuditJobCounts(**counts, total=job["total"]),
        items=items,
    )


@app.post(
    "/api/v1/audit-jobs",
    response_model=AuditJobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["audit-jobs"],
    summary="异步审计：提交单条或 1～20 条清单，立即返回 job_id，后台按序逐项求解落库",
)
def create_audit_job(payload: AuditJobIn, request: Request) -> AuditJobCreatedResponse:
    """清单内容在提交时**不做逐项校验**：非法清单在后台记为该项 ``invalid_manifest``，
    单项失败不阻断其他项。请求体二选一：``manifest``（单条）或 ``manifests``（批量）。
    """
    manifests = payload.raw_manifests()
    job_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn = get_conn(request)
    insert_audit_job(conn, job_id=job_id, created_at=created_at, manifests=manifests)
    request.app.state.job_runner.notify()

    items = [
        AuditJobItemOut(index=index, status="queued")
        for index in range(len(manifests))
    ]
    return AuditJobCreatedResponse(
        job_id=job_id, status="queued", total=len(manifests),
        created_at=created_at, items=items,
    )


@app.get(
    "/api/v1/audit-jobs/{job_id}",
    response_model=AuditJobResponse,
    tags=["audit-jobs"],
    summary="查询异步作业：整体状态、完成计数与逐项结果",
)
def get_audit_job_detail(job_id: str, request: Request) -> AuditJobResponse:
    conn = get_conn(request)
    job = get_audit_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"审计作业不存在: {job_id}")
    return _build_job_response(conn, job)


@app.delete(
    "/api/v1/audit-jobs/{job_id}/items/{item_index}",
    response_model=AuditJobItemOut,
    tags=["audit-jobs"],
    summary="取消作业中尚未开始（queued）的单个条目",
)
def cancel_audit_job_item(
    job_id: str, item_index: int, request: Request
) -> AuditJobItemOut:
    """仅 queued 条目可取消；running/succeeded/failed/cancelled 返回 409。

    取消是“标记为 cancelled”而非物理删除，下标与计数保持稳定。
    """
    if item_index < 0:
        raise HTTPException(status_code=422, detail="item_index 不能为负数")
    conn = get_conn(request)
    job = get_audit_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"审计作业不存在: {job_id}")

    updated = cancel_job_item(conn, job_id, item_index, utcnow_iso())
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"条目不存在: 作业 {job_id} 下标 {item_index}（合法下标 0～{job['total'] - 1}）",
        )
    if updated is False:
        row = get_audit_job_item(conn, job_id, item_index)
        raise HTTPException(
            status_code=409,
            detail=f"条目已开始或已结束，无法取消（当前状态: {row['status']}）",
        )

    # 取消可能恰好使整个作业收尾（例如 queued 作业的全部条目被取消）
    finalize_job_if_done(conn, job_id, utcnow_iso())
    request.app.state.job_runner.notify()
    return _job_item_out(conn, updated)


@app.get("/api/v1/audits", response_model=dict, tags=["audits"])
def get_audits(
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status", pattern="^(feasible|infeasible|inconclusive)$"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict:
    conn = get_conn(request)
    rows = list_audits(conn, status=status_filter, limit=limit, offset=offset)
    total = count_audits(conn, status=status_filter)
    items = [
        AuditSummary(
            id=row["id"],
            created_at=row["created_at"],
            status=row["status"],
            root_plugin=row["root_plugin"],
            dependency_count=row["dependency_count"],
            conflict_count=row["conflict_count"] or 0,
        ).model_dump()
        for row in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get(
    "/api/v1/audits/summary",
    response_model=AuditSummaryResponse,
    tags=["audits"],
    summary="按 UTC 时间桶（day/week）汇总已保存审计结果的状态计数",
)
def get_audits_summary(
    request: Request,
    bucket: str = Query(
        "day",
        pattern="^(day|week)$",
        description="时间桶粒度：day（UTC 自然日）或 week（周一 UTC 开始的自然周）",
    ),
    start: Optional[str] = Query(
        None,
        description=(
            "可选，UTC 区间起始（左闭）：ISO 8601（如 2026-09-01T00:00:00Z）"
            "或日期 YYYY-MM-DD（按 UTC 00:00 计）；省略时取最早记录所在桶起始"
        ),
    ),
    end: Optional[str] = Query(
        None,
        description=(
            "可选，UTC 区间结束（右开）：ISO 8601（如 2026-09-30T00:00:00Z）"
            "或日期 YYYY-MM-DD；省略时取最晚记录所在桶的下一桶起始"
        ),
    ),
) -> AuditSummaryResponse:
    """统计区间 ``[start, end)`` 内**已保存**审计记录，按桶返回
    feasible/infeasible/inconclusive 数量及总数。

    * 区间左闭右开，范围外记录不计入；空桶补零，桶按起始时刻升序；
    * week 桶从**周一 UTC 00:00**开始，与请求参数是否对齐周一无关；
    * 时间格式非法、带非 UTC 时区，或 ``start >= end`` 时返回 `422`；
    * ``start`` / ``end`` 均可省略：无任何记录时返回空结果，
      省略 ``start`` 取最早记录所在桶起始，省略 ``end`` 取最晚记录的下一桶起始。
    """
    start_dt = None
    end_dt = None
    if start is not None:
        try:
            start_dt = parse_utc_bound(start)
        except InvalidTimeBound as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if end is not None:
        try:
            end_dt = parse_utc_bound(end)
        except InvalidTimeBound as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if start_dt is not None and end_dt is not None and start_dt >= end_dt:
        raise HTTPException(
            status_code=422,
            detail=(
                f"非法时间区间：start（{start_dt.isoformat()}）必须早于 "
                f"end（{end_dt.isoformat()}），区间为左闭右开"
            ),
        )

    conn = get_conn(request)
    rows = list_audit_created_status(conn)
    body = build_audit_summary(rows, bucket=bucket, start=start_dt, end=end_dt)
    return AuditSummaryResponse(**body)


@app.get("/api/v1/audits/{audit_id}", response_model=AuditResponse, tags=["audits"])
def get_audit_detail(audit_id: str, request: Request) -> AuditResponse:
    conn = get_conn(request)
    row = get_audit(conn, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"审计记录不存在: {audit_id}")
    result = json.loads(row["result_json"])
    return AuditResponse(**result)


@app.get(
    "/api/v1/audits/impact/{package_name}",
    response_model=ImpactResponse,
    tags=["audits"],
    summary="历史影响反查：哪些审计直接或传递依赖了该包/版本",
    responses={
        200: {
            "description": "命中的审计记录（未知包同样返回 200 与空结果，不返回 404）",
            "content": {
                "application/json": {
                    "example": {
                        "package": "core-lib",
                        "version": None,
                        "limit": 20,
                        "total": 2,
                        "summary": {
                            "total": 2,
                            "feasible": 1,
                            "infeasible": 1,
                            "inconclusive": 0,
                            "resolution_hits": 1,
                            "conflict_hits": 1,
                            "root": 0,
                            "direct": 1,
                            "transitive": 1,
                        },
                        "versions": [
                            {"version": "1.5.0", "total": 1, "feasible": 1, "infeasible": 0},
                            {"version": "2.0.0", "total": 1, "feasible": 0, "infeasible": 1},
                        ],
                        "items": [
                            {
                                "audit_id": "b1f2…",
                                "created_at": "2026-09-22T10:00:00.000000+00:00",
                                "status": "infeasible",
                                "root_plugin": None,
                                "depth": 1,
                                "direct": True,
                                "as_root": False,
                                "versions": ["2.0.0"],
                                "match_types": ["conflict"],
                                "resolution_match": False,
                                "conflict_match": True,
                                "conflict_kinds": ["selection_conflict"],
                                "matches": [
                                    {
                                        "match_type": "conflict",
                                        "role": "constrained",
                                        "depth": 1,
                                        "direct": True,
                                        "as_root": False,
                                        "version": "2.0.0",
                                        "version_range": "^2.0.0",
                                        "conflict_kind": "selection_conflict",
                                        "conflict_package": "core-lib",
                                        "locked_version": "2.0.0",
                                        "minimal_chain": True,
                                        "source_chain": [
                                            {"package": "<manifest>", "version_range": "*", "version": None},
                                            {"package": "loggerkit", "version_range": "=0.9.0", "version": "0.9.0"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        }
    },
)
def get_package_impact(
    package_name: str,
    request: Request,
    version: Optional[str] = Query(
        None,
        description="可选，精确匹配具体版本（需为合法 SemVer）；省略时按包聚合全部版本",
        examples={
            "aggregate": {"summary": "省略：按包聚合", "value": None},
            "pinned": {"summary": "精确版本", "value": "1.5.0"},
        },
    ),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        pattern="^(feasible|infeasible|inconclusive)$",
        description="可选，仅扫描指定状态的审计记录",
    ),
    limit: int = Query(20, ge=1, le=MAX_LIMIT, description="返回记录上限（汇总计数不受影响）"),
) -> ImpactResponse:
    if version is not None:
        try:
            version = str(Version.parse(version))
        except VersionError as exc:
            raise HTTPException(status_code=422, detail=f"非法版本 {version!r}: {exc}") from exc

    conn = get_conn(request)
    rows = iter_audits(conn, status=status_filter)
    body = build_impact(rows, package=package_name, version=version, limit=limit)
    return ImpactResponse(**body)


@app.post(
    "/api/v1/audits/export-bundle",
    response_model=BundleExportResponse,
    tags=["audits"],
)
def export_bundle(payload: BundleExportIn, request: Request) -> BundleExportResponse:
    """把已存在的审计结果打包成确定性、可验证的 JSONL 导出包。

    只读数据库，不写入任何内容。
    """
    audit_ids = payload.audit_ids
    duplicates = sorted({aid for aid in audit_ids if audit_ids.count(aid) > 1})
    if duplicates:
        raise HTTPException(
            status_code=422, detail=f"audit_ids 包含重复 ID: {duplicates}"
        )

    conn = get_conn(request)
    records: list[dict] = []
    for audit_id in audit_ids:
        row = get_audit(conn, audit_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"审计记录不存在: {audit_id}")
        result = json.loads(row["result_json"])
        # 复用现有响应模型序列化，保证字段形态与 GET 详情一致
        records.append(AuditResponse(**result).model_dump(mode="json"))

    bundle = build_bundle(records, include_conflicts=payload.include_conflicts)
    return BundleExportResponse(**bundle)


@app.post(
    "/api/v1/audits/verify-bundle",
    response_model=BundleVerifyResponse,
    tags=["audits"],
)
def verify_bundle_endpoint(payload: BundleVerifyIn) -> BundleVerifyResponse:
    """校验导出包：manifest、记录顺序、逐条摘要与整体摘要。

    纯内存计算，不读写数据库。任何字节或顺序改动都会返回 verified=false。
    """
    if payload.gzip_base64 is not None:
        envelope, error_type = decode_bundle_base64(payload.gzip_base64)
        if error_type is not None:
            return BundleVerifyResponse(
                verified=False, first_error_index=-1, error_type=error_type
            )
        verified, index, error_type, detail = verify_bundle(
            envelope["manifest"], envelope["jsonl"]
        )
        return BundleVerifyResponse(
            verified=verified,
            first_error_index=index,
            error_type=error_type,
            error_detail=detail,
        )

    if payload.jsonl is not None and payload.manifest is not None:
        verified, index, error_type, detail = verify_bundle(
            payload.manifest, payload.jsonl
        )
        return BundleVerifyResponse(
            verified=verified,
            first_error_index=index,
            error_type=error_type,
            error_detail=detail,
        )

    raise HTTPException(
        status_code=422,
        detail="必须提供 gzip_base64，或同时提供 jsonl 与 manifest",
    )


@app.put("/api/v1/registry/packages/{name}", response_model=dict, tags=["registry"])
def put_package(name: str, payload: PackageIn, request: Request) -> dict:
    if name != payload.name:
        raise HTTPException(status_code=422, detail="路径包名与请求体 name 不一致")
    conn = get_conn(request)
    upsert_package(
        conn,
        name,
        [{"version": v.version, "dependencies": v.dependencies} for v in payload.versions],
    )
    return {
        "name": name,
        "versions": [v.version for v in payload.versions],
        "stored": len(payload.versions),
    }


@app.get("/api/v1/registry/packages", response_model=dict, tags=["registry"])
def list_packages(request: Request) -> dict:
    conn = get_conn(request)
    registry = load_registry(conn)
    return {
        "packages": {
            name: {"versions": sorted(versions.keys(), reverse=True), "version_count": len(versions)}
            for name, versions in sorted(registry.items())
        }
    }
