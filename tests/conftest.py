"""pytest 公共夹具：隔离的临时 SQLite 与 TestClient。"""

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_audits.db"
    seed_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "seed_registry.json")
    )
    monkeypatch.setenv("AUDIT_DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_SEED_PATH", seed_path)

    # 延迟导入，确保环境变量先生效
    from app import database, main as main_module

    monkeypatch.setattr(database, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "SEED_PATH", seed_path)

    with TestClient(main_module.app) as c:
        yield c
