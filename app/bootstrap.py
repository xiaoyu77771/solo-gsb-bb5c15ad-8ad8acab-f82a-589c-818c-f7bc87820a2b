"""进程启动初始化：建表与示例注册表播种。

既被 FastAPI 生命周期调用（uvicorn / 测试），也可作为容器启动前的
独立初始化步骤：``python -m app.bootstrap``（幂等，可安全重复执行）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .database import connect, init_db, package_count, upsert_package

logger = logging.getLogger(__name__)


def initialize_database(seed_path: str | None = None, db_path: str | None = None) -> int:
    """建表并在注册表为空时播种，返回写入的包数量（幂等）。"""
    conn = connect(db_path)
    try:
        init_db(conn)
        seeded = 0
        if package_count(conn) == 0:
            seed_file = Path(seed_path or "")
            if seed_path and seed_file.is_file():
                seed = json.loads(seed_file.read_text(encoding="utf-8"))
                for name, payload in seed.items():
                    upsert_package(conn, name, payload["versions"])
                seeded = len(seed)
                logger.info("已播种示例注册表：%d 个包（%s）", seeded, seed_path)
        return seeded
    finally:
        conn.close()


def main() -> None:
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    seeded = initialize_database(
        seed_path=os.environ.get("AUDIT_SEED_PATH", "/app/data/seed_registry.json"),
        db_path=os.environ.get("AUDIT_DB_PATH"),
    )
    if seeded:
        logger.info("数据库初始化完成，播种 %d 个示例包", seeded)
    else:
        logger.info("数据库初始化完成（注册表非空，跳过播种）")


if __name__ == "__main__":
    main()
