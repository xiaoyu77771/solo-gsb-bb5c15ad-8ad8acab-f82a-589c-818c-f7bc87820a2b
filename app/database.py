"""SQLite 持久化层：审计记录与包注册表。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

DEFAULT_DB_PATH = os.environ.get("AUDIT_DB_PATH", os.path.join(os.getcwd(), "data", "audits.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    root_plugin TEXT,
    root_version TEXT,
    dependency_count INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS packages (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS package_versions (
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    dependencies_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (package, version),
    FOREIGN KEY (package) REFERENCES packages(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
);

CREATE TABLE IF NOT EXISTS audit_job_items (
    job_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    manifest_json TEXT NOT NULL,
    audit_id TEXT,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    error_details_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    run_token TEXT,
    PRIMARY KEY (job_id, item_index),
    FOREIGN KEY (job_id) REFERENCES audit_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audits_created_at ON audits(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_items_status ON audit_job_items(job_id, item_index, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON audit_jobs(status, created_at);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------- 注册表 ----------

def upsert_package(
    conn: sqlite3.Connection,
    name: str,
    versions: List[Dict[str, Any]],
) -> None:
    """整包写入（版本按主键合并，重复版本覆盖依赖）。"""
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO packages(name) VALUES (?)", (name,))
        for item in versions:
            conn.execute(
                """
                INSERT INTO package_versions(package, version, dependencies_json)
                VALUES (?, ?, ?)
                ON CONFLICT(package, version) DO UPDATE SET
                    dependencies_json = excluded.dependencies_json
                """,
                (name, item["version"], json.dumps(item.get("dependencies", {}), ensure_ascii=False)),
            )


def load_registry(conn: sqlite3.Connection) -> Dict[str, Dict[str, Dict[str, str]]]:
    """返回 {包名: {版本: {依赖名: 范围}}}。"""
    registry: Dict[str, Dict[str, Dict[str, str]]] = {}
    rows = conn.execute(
        "SELECT package, version, dependencies_json FROM package_versions ORDER BY package, version"
    ).fetchall()
    for row in rows:
        registry.setdefault(row["package"], {})[row["version"]] = json.loads(row["dependencies_json"])
    return registry


def package_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0])


# ---------- 审计记录 ----------

def insert_audit(
    conn: sqlite3.Connection,
    audit_id: str,
    created_at: str,
    status: str,
    root_plugin: Optional[str],
    root_version: Optional[str],
    dependency_count: int,
    manifest: Dict[str, Any],
    result: Dict[str, Any],
    node_count: int,
) -> None:
    with transaction(conn):
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
                status,
                root_plugin,
                root_version,
                dependency_count,
                json.dumps(manifest, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                node_count,
            ),
        )


def get_audit(conn: sqlite3.Connection, audit_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM audits WHERE id = ?", (audit_id,)).fetchone()


def list_audits(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT id, created_at, status, root_plugin, dependency_count, node_count,
                   json_array_length(json_extract(result_json, '$.conflicts')) AS conflict_count
            FROM audits WHERE status = ?
            ORDER BY created_at DESC, id
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, created_at, status, root_plugin, dependency_count, node_count,
               json_array_length(json_extract(result_json, '$.conflicts')) AS conflict_count
        FROM audits
        ORDER BY created_at DESC, id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def count_audits(conn: sqlite3.Connection, status: Optional[str] = None) -> int:
    if status:
        return int(
            conn.execute("SELECT COUNT(*) FROM audits WHERE status = ?", (status,)).fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0])


def iter_audits(conn: sqlite3.Connection, status: Optional[str] = None) -> Iterator[sqlite3.Row]:
    """遍历全部审计记录（按创建时间倒序），供全量扫描类查询使用。"""
    if status:
        yield from conn.execute(
            "SELECT * FROM audits WHERE status = ? ORDER BY created_at DESC, id",
            (status,),
        )
    else:
        yield from conn.execute("SELECT * FROM audits ORDER BY created_at DESC, id")


def list_audit_created_status(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """返回全部审计记录的 (created_at, status)，供时间桶统计使用。

    区间过滤与分桶在内存中完成（ISO 字符串可能因微秒精度不同而无法直接
    按字典序比较），因此这里只做轻量的全量取数。
    """
    return [
        (row["created_at"], row["status"])
        for row in conn.execute("SELECT created_at, status FROM audits")
    ]


# ---------- 异步审计作业 ----------


def insert_audit_job(
    conn: sqlite3.Connection, job_id: str, created_at: str, manifests: List[Any]
) -> None:
    """创建作业及其全部条目（单事务），条目初始均为 queued。"""
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit_jobs(id, created_at, total, status) VALUES (?, ?, ?, 'queued')",
            (job_id, created_at, len(manifests)),
        )
        conn.executemany(
            """
            INSERT INTO audit_job_items(job_id, item_index, status, manifest_json)
            VALUES (?, ?, 'queued', ?)
            """,
            [
                (job_id, index, json.dumps(manifest, ensure_ascii=False))
                for index, manifest in enumerate(manifests)
            ],
        )


def get_audit_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_jobs WHERE id = ?", (job_id,)).fetchone()


def list_audit_job_items(conn: sqlite3.Connection, job_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_job_items WHERE job_id = ? ORDER BY item_index",
        (job_id,),
    ).fetchall()


def get_audit_job_item(
    conn: sqlite3.Connection, job_id: str, item_index: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_job_items WHERE job_id = ? AND item_index = ?",
        (job_id, item_index),
    ).fetchone()


# 终态条目状态：不再被 worker 处理
_JOB_ITEM_TERMINAL_STATES = ("succeeded", "failed", "cancelled")


def claim_next_job_item(
    conn: sqlite3.Connection, run_token: str, now: str
) -> Optional[Tuple[str, int, Any]]:
    """领取下一个待处理条目：作业按 FIFO，作业内按下标顺序。

    原子地把选中条目标记为 running（并记录 run_token），同时把其所属
    queued 作业切换到 running。返回 (job_id, item_index, manifest)；
    没有待处理条目时返回 None。调用方传入的 run_token 同时写入条目，
    后续完成时需凭同一 token 才能提交，防止重启后旧执行者覆盖新执行者。
    """
    with transaction(conn):
        row = conn.execute(
            """
            SELECT ji.job_id, ji.item_index, ji.manifest_json
            FROM audit_job_items AS ji
            JOIN audit_jobs AS j ON j.id = ji.job_id
            WHERE ji.status = 'queued'
              AND j.status IN ('queued', 'running')
            ORDER BY j.created_at ASC, ji.item_index ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        updated = conn.execute(
            "UPDATE audit_job_items SET status='running', run_token=?, started_at=? "
            "WHERE job_id=? AND item_index=? AND status='queued'",
            (run_token, now, row["job_id"], row["item_index"]),
        )
        if updated.rowcount == 0:  # 极端竞争下被他人抢先
            return None
        conn.execute(
            "UPDATE audit_jobs SET status='running', started_at=COALESCE(started_at, ?) "
            "WHERE id=? AND status IN ('queued', 'running')",
            (now, row["job_id"]),
        )
        return row["job_id"], int(row["item_index"]), json.loads(row["manifest_json"])


def complete_job_item_failure(
    conn: sqlite3.Connection,
    job_id: str,
    item_index: int,
    run_token: str,
    error_code: str,
    error_message: str,
    error_details: Optional[List[Dict[str, Any]]],
    now: str,
) -> bool:
    """把条目标记为失败（结构化错误），仅当 run_token 仍匹配。"""
    with transaction(conn):
        row = conn.execute(
            "SELECT status, run_token FROM audit_job_items WHERE job_id=? AND item_index=?",
            (job_id, item_index),
        ).fetchone()
        if row is None or row["status"] != "running" or row["run_token"] != run_token:
            return False
        conn.execute(
            "UPDATE audit_job_items SET status='failed', error_code=?, error_message=?, "
            "error_details_json=?, finished_at=?, run_token=NULL "
            "WHERE job_id=? AND item_index=?",
            (
                error_code,
                error_message,
                json.dumps(error_details, ensure_ascii=False) if error_details is not None else None,
                now,
                job_id,
                item_index,
            ),
        )
    return True


def cancel_job_item(
    conn: sqlite3.Connection, job_id: str, item_index: int, now: str
) -> Optional[sqlite3.Row] | bool:
    """取消尚未开始（queued）的条目。

    返回取消后的条目行；条目不存在返回 None；已开始/已结束返回 ``False``
    （调用方据此映射为 409）。
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM audit_job_items WHERE job_id=? AND item_index=?",
            (job_id, item_index),
        ).fetchone()
        if row is None:
            return None
        if row["status"] != "queued":
            return False  # type: ignore[return-value]
        conn.execute(
            "UPDATE audit_job_items SET status='cancelled', finished_at=? "
            "WHERE job_id=? AND item_index=? AND status='queued'",
            (now, job_id, item_index),
        )
    return get_audit_job_item(conn, job_id, item_index)


def count_job_items(
    conn: sqlite3.Connection, job_id: str
) -> Dict[str, int]:
    """按状态统计条目数，键覆盖全部六种状态。"""
    counts = {state: 0 for state in ("queued", "running") + _JOB_ITEM_TERMINAL_STATES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM audit_job_items WHERE job_id=? GROUP BY status",
        (job_id,),
    ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def finalize_job_if_done(
    conn: sqlite3.Connection, job_id: str, now: str
) -> Optional[str]:
    """作业的全部条目都到终态后，把作业切换到终态。

    有 failed 条目 -> failed；否则 succeeded（全部 cancelled 也算完成）。
    返回最终作业状态；尚未全部结束返回 None。
    """
    with transaction(conn):
        job = conn.execute(
            "SELECT status FROM audit_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None or job["status"] not in ("queued", "running"):
            return job["status"] if job else None
        pending = conn.execute(
            "SELECT COUNT(*) FROM audit_job_items "
            "WHERE job_id=? AND status IN ('queued', 'running')",
            (job_id,),
        ).fetchone()[0]
        if pending:
            return None
        failed = conn.execute(
            "SELECT COUNT(*) FROM audit_job_items WHERE job_id=? AND status='failed'",
            (job_id,),
        ).fetchone()[0]
        final_status = "failed" if failed else "succeeded"
        conn.execute(
            "UPDATE audit_jobs SET status=?, finished_at=? WHERE id=?",
            (final_status, now, job_id),
        )
        return final_status


def recover_unfinished_jobs(conn: sqlite3.Connection, now: str) -> None:
    """服务重启后的状态恢复：

    * 上次崩溃时停在 running 的条目重置为 queued（run_token 作废），
      旧 worker 线程即使残留也无法用过期 token 完成它；
    * 全部条目都已到终态但作业仍处于 queued/running 的，补登终态；
    * 其余未完成作业重新暴露给 worker，按 FIFO 继续执行。
    """
    with transaction(conn):
        conn.execute(
            "UPDATE audit_job_items SET status='queued', started_at=NULL, "
            "finished_at=NULL, run_token=NULL WHERE status='running'"
        )
        rows = conn.execute(
            "SELECT id FROM audit_jobs WHERE status IN ('queued', 'running')"
        ).fetchall()
        job_ids = [row["id"] for row in rows]
    for job_id in job_ids:
        finalize_job_if_done(conn, job_id, now)
