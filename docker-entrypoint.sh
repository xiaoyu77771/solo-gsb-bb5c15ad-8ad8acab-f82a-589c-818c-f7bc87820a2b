#!/bin/sh
# 容器启动初始化（幂等）：
# 1. 建表（audit_jobs / audit_job_items 等）；
# 2. 仅当注册表为空时播种示例包；
# 3. 随后启动 uvicorn；未完成作业的恢复在应用生命周期内完成。
set -e

echo "[entrypoint] 初始化数据库: ${AUDIT_DB_PATH:-/data/audits.db}"
python -m app.bootstrap

exec "$@"
