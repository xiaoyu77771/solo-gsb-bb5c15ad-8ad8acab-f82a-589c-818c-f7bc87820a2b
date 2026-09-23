"""异步审计作业 POST/GET/DELETE /api/v1/audit-jobs 的端到端测试。"""

from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

FEASIBLE = {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}
INFEASIBLE = {
    "dependencies": [
        {"name": "cachekit", "range": "^1.0.0"},
        {"name": "loggerkit", "range": "^0.9.0"},
    ]
}
INVALID = {"dependencies": [{"name": "x", "range": "not-valid"}]}
SEED_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "seed_registry.json")
)


def _poll_job(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while True:
        resp = client.get(f"/api/v1/audit-jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        assert time.time() < deadline, f"作业 {job_id} 未在 {timeout}s 内结束: {body['status']}"
        time.sleep(0.02)


# ---------- 提交与逐项执行 ----------


def test_job_single_manifest_returns_immediately_then_succeeds(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifest": FEASIBLE})
    assert resp.status_code == 202, resp.text
    created = resp.json()
    assert created["job_id"]
    assert created["status"] == "queued"
    assert created["total"] == 1
    assert created["created_at"]
    assert [item["status"] for item in created["items"]] == ["queued"]
    assert created["items"][0]["index"] == 0

    body = _poll_job(client, created["job_id"])
    assert body["status"] == "succeeded"
    assert body["started_at"]
    assert body["finished_at"]
    counts = body["counts"]
    assert counts == {
        "total": 1, "queued": 0, "running": 0,
        "succeeded": 1, "failed": 0, "cancelled": 0,
    }

    item = body["items"][0]
    assert item["status"] == "succeeded"
    assert item["error"] is None
    assert item["audit_id"]
    assert item["audit"]["id"] == item["audit_id"]
    assert item["audit"]["status"] == "feasible"

    # 成功项与同步接口等价：独立审计记录可经 GET /audits/{id} 取回
    detail = client.get(f"/api/v1/audits/{item['audit_id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == item["audit_id"]


def test_job_batch_mixed_results_isolated_and_persisted(client):
    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [FEASIBLE, INFEASIBLE, INVALID]},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert resp.json()["total"] == 3

    body = _poll_job(client, job_id)
    # 存在失败条目：作业整体 failed，但其余项不受影响
    assert body["status"] == "failed"
    assert body["counts"] == {
        "total": 3, "queued": 0, "running": 0,
        "succeeded": 2, "failed": 1, "cancelled": 0,
    }

    first, second, third = body["items"]
    assert [item["index"] for item in body["items"]] == [0, 1, 2]

    assert first["status"] == "succeeded"
    assert first["audit"]["status"] == "feasible"

    assert second["status"] == "succeeded"
    assert second["audit"]["status"] == "infeasible"
    assert len(second["audit"]["conflicts"]) >= 2

    assert third["status"] == "failed"
    assert third["audit_id"] is None
    assert third["audit"] is None
    assert third["error"]["code"] == "invalid_manifest"
    assert third["error"]["details"], "非法清单必须携带结构化校验细节"

    # 仅成功项产生审计记录
    listed = client.get("/api/v1/audits?limit=100").json()
    assert listed["total"] == 2


def test_job_solver_exception_fails_items_only(client, monkeypatch):
    """单项求解异常只把该条记为 failed，不阻断其他项。"""
    from app import jobs as jobs_module

    def boom(self, root_dependencies, root_name=None, root_version=None):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(jobs_module.Solver, "solve", boom)

    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [FEASIBLE, FEASIBLE]},
    )
    job_id = resp.json()["job_id"]
    body = _poll_job(client, job_id)

    assert body["status"] == "failed"
    assert body["counts"]["failed"] == 2
    assert all(item["status"] == "failed" for item in body["items"])
    assert all(item["error"]["code"] == "solver_error" for item in body["items"])
    assert all(item["audit_id"] is None for item in body["items"])
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_job_missing_root_plugin_is_item_failure(client):
    resp = client.post(
        "/api/v1/audit-jobs",
        json={
            "manifests": [
                {"plugin_name": "ghost", "plugin_version": "9.9.9", "dependencies": []},
                FEASIBLE,
            ]
        },
    )
    body = _poll_job(client, resp.json()["job_id"])
    assert body["status"] == "failed"
    bad, good = body["items"]
    assert bad["status"] == "failed"
    assert bad["error"]["code"] == "invalid_manifest"
    assert "ghost" in bad["error"]["message"]
    assert good["status"] == "succeeded"
    assert good["audit_id"]


# ---------- 请求校验 ----------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"manifest": FEASIBLE, "manifests": [FEASIBLE]},
        {"manifests": []},
        {"manifests": [FEASIBLE] * 21},
        {"manifests": "not-a-list"},
        {"manifest": None, "manifests": None},
    ],
)
def test_job_bad_payload_422(client, payload):
    resp = client.post("/api/v1/audit-jobs", json=payload)
    assert resp.status_code == 422, resp.text
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_job_batch_boundary_20_accepted(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifests": [FEASIBLE] * 20})
    assert resp.status_code == 202, resp.text
    body = _poll_job(client, resp.json()["job_id"])
    assert body["counts"]["total"] == 20
    assert body["counts"]["succeeded"] == 20
    assert body["status"] == "succeeded"


def test_job_malformed_manifest_shape_is_item_failure(client):
    """元素不是对象、字段类型错误等只挂在该项，不影响其他项。"""
    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [["not", "an", "object"], FEASIBLE, {"dependencies": "x"}]},
    )
    body = _poll_job(client, resp.json()["job_id"])
    assert body["status"] == "failed"
    assert body["counts"]["failed"] == 2
    assert body["counts"]["succeeded"] == 1
    assert [item["error"]["code"] for item in body["items"] if item["error"]] == [
        "invalid_manifest",
        "invalid_manifest",
    ]


def test_get_unknown_job_404(client):
    assert client.get("/api/v1/audit-jobs/nope").status_code == 404


# ---------- DELETE 取消 ----------


def _make_gate(real_solve):
    """构造一个带 entered/release 事件的 Solver.solve 替身（函数，可被正常绑定）。"""

    entered = threading.Event()
    release = threading.Event()

    def gated_solve(solver_self, root_dependencies, root_name=None, root_version=None):
        entered.set()
        assert release.wait(timeout=15), "worker 求解未被及时放行"
        return real_solve(solver_self, root_dependencies, root_name, root_version)

    gated_solve.entered = entered
    gated_solve.release = release
    return gated_solve


def test_cancel_queued_item_only_and_409_when_started(client, monkeypatch):
    from app import jobs as jobs_module

    real_solve = jobs_module.Solver.solve
    gate = _make_gate(real_solve)
    monkeypatch.setattr(jobs_module.Solver, "solve", gate)

    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [FEASIBLE, FEASIBLE, FEASIBLE]},
    )
    job_id = resp.json()["job_id"]

    # 等待第 0 项进入 running，第 1 项仍 queued
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/api/v1/audit-jobs/{job_id}").json()
        if body["items"][0]["status"] == "running":
            break
        time.sleep(0.01)
    body = client.get(f"/api/v1/audit-jobs/{job_id}").json()
    assert body["items"][0]["status"] == "running"
    assert body["items"][1]["status"] == "queued"

    # 取消尚未开始的第 1 项
    cancelled = client.delete(f"/api/v1/audit-jobs/{job_id}/items/1")
    assert cancelled.status_code == 200, cancelled.text
    item = cancelled.json()
    assert item["status"] == "cancelled"
    assert item["index"] == 1
    assert item["audit_id"] is None

    # 作业进行中：仍有 queued/running 条目，作业保持 running
    body = client.get(f"/api/v1/audit-jobs/{job_id}").json()
    assert body["status"] == "running"
    assert body["counts"]["cancelled"] == 1

    # running 项不可取消（409），已取消项重复取消同样 409
    conflict = client.delete(f"/api/v1/audit-jobs/{job_id}/items/0")
    assert conflict.status_code == 409
    again = client.delete(f"/api/v1/audit-jobs/{job_id}/items/1")
    assert again.status_code == 409

    # 越界下标 / 未知作业 / 负数下标
    assert client.delete(f"/api/v1/audit-jobs/{job_id}/items/99").status_code == 404
    assert client.delete(f"/api/v1/audit-jobs/nope/items/0").status_code == 404
    assert client.delete(f"/api/v1/audit-jobs/{job_id}/items/-1").status_code == 422

    # 放行 worker：第 0、2 项成功，被取消的第 1 项不参与
    gate.release.set()
    body = _poll_job(client, job_id)
    assert body["status"] == "succeeded"
    assert body["counts"] == {
        "total": 3, "queued": 0, "running": 0,
        "succeeded": 2, "failed": 0, "cancelled": 1,
    }
    assert body["items"][1]["status"] == "cancelled"
    assert client.get("/api/v1/audits?limit=100").json()["total"] == 2


def test_cancel_all_queued_items_finalizes_job(client, monkeypatch):
    """作业尚未开始时取消全部条目：作业直接收尾为 succeeded（cancelled=total）。"""
    from app import jobs as jobs_module

    real_solve = jobs_module.Solver.solve
    gate = _make_gate(real_solve)
    monkeypatch.setattr(jobs_module.Solver, "solve", gate)

    # 先让 worker 忙在另一个作业上，保证本作业整体处于 queued
    first = client.post("/api/v1/audit-jobs", json={"manifest": FEASIBLE})
    assert gate.entered.wait(10)

    second = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [FEASIBLE, FEASIBLE]},
    )
    second_id = second.json()["job_id"]

    for index in (0, 1):
        resp = client.delete(f"/api/v1/audit-jobs/{second_id}/items/{index}")
        assert resp.status_code == 200, resp.text

    body = client.get(f"/api/v1/audit-jobs/{second_id}").json()
    assert body["status"] == "succeeded"
    assert body["counts"]["cancelled"] == 2
    assert body["finished_at"]

    gate.release.set()
    first_body = _poll_job(client, first.json()["job_id"])
    assert first_body["status"] == "succeeded"
    # 第一个作业不受第二个作业取消影响
    assert client.get("/api/v1/audits?limit=100").json()["total"] == 1


# ---------- 服务重启恢复 ----------


def _fresh_client(tmp_path, monkeypatch, db_name):
    db_path = tmp_path / db_name
    monkeypatch.setenv("AUDIT_DB_PATH", str(db_path))
    from app import database, main as main_module

    monkeypatch.setattr(database, "DEFAULT_DB_PATH", str(db_path), raising=False)
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", str(db_path), raising=False)
    monkeypatch.setattr(main_module, "SEED_PATH", SEED_PATH, raising=False)
    monkeypatch.setattr(main_module, "JOB_STOP_TIMEOUT", 0.1, raising=False)
    return TestClient(main_module.app), str(db_path)


def test_job_recovers_running_item_after_restart(tmp_path, monkeypatch):
    """处理中重启：running 条目重置为 queued 并被新 worker 重跑完成。

    旧进程残留的 worker 线程持有过期 run_token，放行后其写入必须被拒绝，
    不产生重复审计记录。
    """
    from app import jobs as jobs_module

    client, _ = _fresh_client(tmp_path, monkeypatch, "running-recover.db")

    real_solve = jobs_module.Solver.solve
    gate = _make_gate(real_solve)
    monkeypatch.setattr(jobs_module.Solver, "solve", gate)

    with client as c1:
        resp = c1.post(
            "/api/v1/audit-jobs",
            json={"manifests": [FEASIBLE, FEASIBLE]},
        )
        job_id = resp.json()["job_id"]
        assert gate.entered.wait(10)

        body = c1.get(f"/api/v1/audit-jobs/{job_id}").json()
        assert body["status"] == "running"
        assert body["items"][0]["status"] == "running"
        assert body["items"][1]["status"] == "queued"
        # 作业状态已落盘：重启恢复完全依赖 SQLite
        assert c1.get("/api/v1/audits").json()["total"] == 0

    # 关闭 c1：旧 worker 因 0.1s 超时仍阻塞在 gate 中（daemon 线程残留）

    # 用真实求解器启动“第二个进程”：running 条目应被重置并继续完成
    monkeypatch.setattr(jobs_module.Solver, "solve", real_solve)
    client2, db_path = _fresh_client(tmp_path, monkeypatch, "running-recover.db")
    assert db_path.endswith("running-recover.db")
    with client2 as c2:
        deadline = time.time() + 15
        while time.time() < deadline:
            body = c2.get(f"/api/v1/audit-jobs/{job_id}").json()
            if body["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        body = c2.get(f"/api/v1/audit-jobs/{job_id}").json()
        assert body["status"] == "succeeded", body
        assert body["counts"]["succeeded"] == 2
        assert body["counts"]["running"] == 0
        assert body["counts"]["queued"] == 0
        assert all(item["audit_id"] for item in body["items"])
        # 过期 token 的旧执行者不得制造重复审计
        assert c2.get("/api/v1/audits?limit=100").json()["total"] == 2

    # 最后放行旧线程，让它用过期 token 自行失败退出
    gate.release.set()
    time.sleep(0.2)


def test_job_recovers_queued_job_after_restart(tmp_path, monkeypatch):
    """从未开始的作业（整体 queued）重启后按 FIFO 继续执行。"""
    from app import jobs as jobs_module

    client, _ = _fresh_client(tmp_path, monkeypatch, "queued-recover.db")

    real_claim = jobs_module.claim_next_job_item
    monkeypatch.setattr(jobs_module, "claim_next_job_item", lambda *a, **k: None)

    with client as c1:
        resp = c1.post(
            "/api/v1/audit-jobs",
            json={"manifests": [FEASIBLE, INFEASIBLE]},
        )
        job_id = resp.json()["job_id"]
        body = c1.get(f"/api/v1/audit-jobs/{job_id}").json()
        assert body["status"] == "queued"
        assert body["counts"]["queued"] == 2

    # 重启：恢复领取能力，作业被继续执行
    monkeypatch.setattr(jobs_module, "claim_next_job_item", real_claim)
    client2, _ = _fresh_client(tmp_path, monkeypatch, "queued-recover.db")
    with client2 as c2:
        deadline = time.time() + 15
        while time.time() < deadline:
            body = c2.get(f"/api/v1/audit-jobs/{job_id}").json()
            if body["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        body = c2.get(f"/api/v1/audit-jobs/{job_id}").json()
        assert body["status"] == "succeeded", body
        assert body["counts"]["succeeded"] == 2
        statuses = {item["index"]: item["audit"]["status"] for item in body["items"]}
        assert statuses == {0: "feasible", 1: "infeasible"}
