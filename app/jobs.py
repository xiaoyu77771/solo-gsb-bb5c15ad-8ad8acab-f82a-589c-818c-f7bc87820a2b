"""异步审计作业：单后台线程按 FIFO 顺序逐项求解、落库。

设计要点：

* 全部作业状态存于 SQLite（``audit_jobs`` / ``audit_job_items``），
  进程重启后由 :func:`recover_unfinished_jobs` 恢复未完成作业；
* 每个条目领取时写入一次性 ``run_token``，完成时要求 token 仍匹配——
  旧进程残留的 worker 线程无法用过期 token 覆盖重启后重跑的条目；
* 单项校验/求解/落库失败只标记该条为 failed，绝不阻断后续条目；
* 作业内全部条目到达终态后，作业收尾为 succeeded（有 failed 条目则 failed）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from typing import Any, Callable, Optional, Tuple

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from . import database as db
from .database import (
    DEFAULT_DB_PATH,
    claim_next_job_item,
    complete_job_item_failure,
    connect,
    finalize_job_if_done,
    load_registry,
    recover_unfinished_jobs,
    utcnow_iso,
)
from .models import ManifestIn
from .solver import Solver

logger = logging.getLogger(__name__)


class _RootPluginNotFound(Exception):
    """根插件版本不存在于注册表。"""


def _root_label(manifest: ManifestIn) -> Optional[str]:
    if not manifest.plugin_name:
        return None
    if manifest.plugin_version:
        return f"{manifest.plugin_name}@{manifest.plugin_version}"
    return manifest.plugin_name


def _solve_manifest(
    manifest: ManifestIn, registry: dict
) -> Tuple[Any, Optional[str], Optional[str]]:
    """与同步接口相同的内存态校验 + 求解（与 main._solve_manifest 保持一致）。"""
    manifest.validate_root_consistency()

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
) -> Tuple[str, str, dict]:
    audit_id = str(uuid.uuid4())
    created_at = utcnow_iso()
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


def _process_item(
    conn: sqlite3.Connection, raw_manifest: Any
) -> Tuple[ManifestIn, Any, str, str, dict, Optional[str], Optional[str]]:
    """单条目的校验 + 求解（纯内存，不落库）。

    返回值与 main 模块同名辅助函数的组合等价；失败时抛出 ValidationError /
    _RootPluginNotFound / ValueError / Exception，由 :meth:`JobRunner._run_item` 分类。
    """
    manifest = ManifestIn.model_validate(raw_manifest)
    registry = load_registry(conn)
    result, root_label, root_version = _solve_manifest(manifest, registry)
    audit_id, created_at, body = _build_audit_body(manifest, result, root_label, root_version)
    return manifest, result, audit_id, created_at, body, root_label, root_version


class JobRunner:
    """后台作业执行器：一个 daemon 线程 + 独立 SQLite 连接。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        idle_wait: float = 0.2,
        stop_timeout: float = 10.0,
        process_item: Callable[
            [sqlite3.Connection, Any],
            Tuple[ManifestIn, Any, str, str, dict, Optional[str], Optional[str]],
        ] = _process_item,
    ) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._idle_wait = idle_wait
        self._stop_timeout = stop_timeout
        self._process_item = process_item
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[sqlite3.Connection] = None

    # ---------- 生命周期 ----------

    def recover(self) -> None:
        """重启恢复：running 条目回到 queued，已完成作业补登终态。"""
        conn = connect(self._db_path)
        try:
            recover_unfinished_jobs(conn, utcnow_iso())
        finally:
            conn.close()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="audit-job-worker", daemon=True
        )
        self._thread.start()

    def notify(self) -> None:
        """新作业写入或条目被取消后唤醒 worker。"""
        self._wake.set()

    def stop(self, timeout: Optional[float] = None) -> None:
        """请求停止并等待当前条目处理完（默认等待 stop_timeout 秒）。"""
        self._stop_event.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout if timeout is not None else self._stop_timeout)
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------- 后台循环 ----------

    def _run(self) -> None:
        # 连接在 worker 线程内创建，仅由该线程使用，避免跨线程共享
        self._conn = connect(self._db_path)
        try:
            while not self._stop_event.is_set():
                run_token = str(uuid.uuid4())
                claimed = claim_next_job_item(self._conn, run_token, utcnow_iso())
                if claimed is None:
                    self._wake.wait(timeout=self._idle_wait)
                    self._wake.clear()
                    continue
                job_id, item_index, raw_manifest = claimed
                self._run_item(
                    self._conn, job_id, item_index, raw_manifest, run_token
                )
        except Exception:  # noqa: BLE001 - worker 线程永不静默崩溃
            logger.exception("审计作业 worker 异常退出")
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------- 单条目处理 ----------

    def _fail_item(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        item_index: int,
        run_token: str,
        code: str,
        message: str,
        details: Optional[list] = None,
    ) -> None:
        if complete_job_item_failure(
            conn, job_id, item_index, run_token, code, message, details, utcnow_iso()
        ):
            finalize_job_if_done(conn, job_id, utcnow_iso())

    def _run_item(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        item_index: int,
        raw_manifest: Any,
        run_token: str,
    ) -> None:
        """处理单个条目：校验/求解/落库，任何失败只影响该条。"""
        # ---- 阶段一：逐项校验 + 求解（纯内存） ----
        try:
            manifest, result, audit_id, created_at, body, root_label, root_version = (
                self._process_item(conn, raw_manifest)
            )
        except ValidationError as exc:
            self._fail_item(
                conn,
                job_id,
                item_index,
                run_token,
                "invalid_manifest",
                "清单校验失败",
                jsonable_encoder(exc.errors()),
            )
            return
        except (_RootPluginNotFound, ValueError) as exc:
            self._fail_item(
                conn, job_id, item_index, run_token, "invalid_manifest", str(exc)
            )
            return
        except Exception:  # noqa: BLE001 - 单项求解异常不得影响其他项
            logger.exception("作业 %s 第 %d 项求解失败", job_id, item_index)
            self._fail_item(
                conn,
                job_id,
                item_index,
                run_token,
                "solver_error",
                "求解过程中发生内部错误",
            )
            return

        # ---- 阶段二：审计落库 + 条目标成功（单事务，凭 run_token 防重启覆盖） ----
        now = utcnow_iso()
        try:
            with db.transaction(conn):
                claimed_row = conn.execute(
                    "SELECT status, run_token FROM audit_job_items "
                    "WHERE job_id=? AND item_index=?",
                    (job_id, item_index),
                ).fetchone()
                if (
                    claimed_row is None
                    or claimed_row["status"] != "running"
                    or claimed_row["run_token"] != run_token
                ):
                    # 条目已被重启恢复或取消：回滚审计写入，交给新的执行者
                    raise _StaleToken()

                # 直接 INSERT（而非 insert_audit），使审计记录与条目标成功
                # 在同一事务内提交：中途崩溃时两者都不生效，重启后整体重跑。
                conn.execute(
                    """
                    INSERT INTO audits(
                        id, created_at, status, root_plugin, root_version,
                        dependency_count, manifest_json, result_json, node_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_id,
                        created_at,
                        result.status,
                        root_label,
                        root_version,
                        len(manifest.dependencies),
                        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False),
                        json.dumps(body, ensure_ascii=False),
                        result.node_count,
                    ),
                )
                conn.execute(
                    "UPDATE audit_job_items SET status='succeeded', audit_id=?, result_json=?, "
                    "error_code=NULL, error_message=NULL, error_details_json=NULL, "
                    "finished_at=?, run_token=NULL WHERE job_id=? AND item_index=?",
                    (audit_id, json.dumps(body, ensure_ascii=False), now, job_id, item_index),
                )
        except _StaleToken:
            return
        except Exception:  # noqa: BLE001 - 单项落库异常不得影响其他项
            logger.exception("作业 %s 第 %d 项持久化失败", job_id, item_index)
            self._fail_item(
                conn,
                job_id,
                item_index,
                run_token,
                "internal_error",
                "审计结果持久化失败",
            )
            return

        finalize_job_if_done(conn, job_id, utcnow_iso())


class _StaleToken(Exception):
    """条目已不属于本次执行（重启恢复后被重跑，或被取消），放弃写入。"""
