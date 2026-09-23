"""批量审计接口 POST /api/v1/audits/batch 的端到端测试。"""

import pytest


def test_batch_mixed_results_isolated_and_persisted(client):
    """feasible / infeasible / 校验失败混合：逐项隔离、计数正确、成功项落库。"""
    payload = {
        "manifests": [
            {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
            {
                "dependencies": [
                    {"name": "cachekit", "range": "^1.0.0"},
                    {"name": "loggerkit", "range": "^0.9.0"},
                ]
            },
            {"dependencies": [{"name": "x", "range": "not-valid"}]},
            {"plugin_name": "megaplugin", "plugin_version": "1.0.0", "dependencies": []},
        ]
    }
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 4
    assert body["feasible"] == 2
    assert body["infeasible"] == 1
    assert body["inconclusive"] == 0
    assert body["error"] == 1

    # 顺序与输入一致
    assert [item["index"] for item in body["items"]] == [0, 1, 2, 3]

    first, second, third, fourth = body["items"]

    # 第 0 项：可行，携带完整结果
    assert first["audit_id"]
    assert first["status"] == "feasible"
    assert first["error"] is None
    assert first["audit"]["id"] == first["audit_id"]
    assert first["audit"]["resolution"]["core-lib"].startswith("1.")

    # 第 1 项：无解
    assert second["status"] == "infeasible"
    assert len(second["audit"]["conflicts"]) >= 2

    # 第 2 项：校验失败（非法范围），结构化 error，无 audit_id
    assert third["audit_id"] is None
    assert third["status"] is None
    assert third["audit"] is None
    assert third["error"]["code"] == "invalid_manifest"
    assert third["error"]["details"], "必须携带结构化校验细节"

    # 第 3 项：即使前一项失败，根插件清单仍正常求解
    assert fourth["status"] == "feasible"
    assert fourth["audit"]["resolution"]["megaplugin"] == "1.0.0"

    # 成功项分别持久化，可用单条详情接口取回
    for item in (first, second, fourth):
        detail = client.get(f"/api/v1/audits/{item['audit_id']}").json()
        assert detail["id"] == item["audit_id"]
        assert detail["status"] == item["status"]

    # 失败项不产生审计记录
    listed = client.get("/api/v1/audits?limit=100").json()
    assert listed["total"] == 3
    assert all(item["status"] != "error" for item in listed["items"])


def test_batch_missing_root_plugin_is_item_error_not_422(client):
    payload = {
        "manifests": [
            {"plugin_name": "ghost", "plugin_version": "9.9.9", "dependencies": []},
            {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]},
        ]
    }
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] == 1
    assert body["feasible"] == 1

    bad, good = body["items"]
    assert bad["error"]["code"] == "invalid_manifest"
    assert "ghost" in bad["error"]["message"]
    assert good["audit_id"]


def test_batch_root_name_version_mismatch_is_item_error(client):
    payload = {"manifests": [{"plugin_name": "megaplugin", "dependencies": []}]}
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["error"]["code"] == "invalid_manifest"
    assert "plugin_name" in item["error"]["message"]


def test_batch_malformed_manifest_shape_is_item_error(client):
    """元素不是对象 / 字段类型错误等整体 schema 失败，同样只挂在该项上。"""
    payload = {
        "manifests": [
            ["not", "an", "object"],
            {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
            {"dependencies": "should-be-a-list"},
        ]
    }
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["feasible"] == 1
    assert body["error"] == 2
    assert [item["error"]["code"] for item in body["items"] if item["error"]] == [
        "invalid_manifest",
        "invalid_manifest",
    ]


def test_batch_empty_list_422(client):
    resp = client.post("/api/v1/audits/batch", json={"manifests": []})
    assert resp.status_code == 422


def test_batch_too_many_items_422(client):
    manifests = [{"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}] * 21
    resp = client.post("/api/v1/audits/batch", json={"manifests": manifests})
    assert resp.status_code == 422
    # 超过上限时整体拒绝，不写入任何记录
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_batch_boundary_20_items_accepted(client):
    manifests = [{"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}] * 20
    resp = client.post("/api/v1/audits/batch", json={"manifests": manifests})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 20
    assert body["feasible"] == 20
    assert len(body["items"]) == 20


@pytest.mark.parametrize("bad_body", [{}, {"manifests": None}, {"wrong": []}])
def test_batch_missing_field_422(client, bad_body):
    resp = client.post("/api/v1/audits/batch", json=bad_body)
    assert resp.status_code == 422


def test_batch_solver_exception_is_isolated(client, monkeypatch):
    """某项求解器抛异常时，仅该项报 solver_error，其他项照常落库。"""
    from app import main as main_module

    def boom(self, root_dependencies, root_name=None, root_version=None):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(main_module.Solver, "solve", boom)

    payload = {
        "manifests": [
            {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
            {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]},
        ]
    }
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["error"] == 2
    assert all(item["error"]["code"] == "solver_error" for item in body["items"])
    assert all(item["audit_id"] is None for item in body["items"])
    # 没有任何成功项落库
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_batch_persist_failure_does_not_rollback_other_items(client, monkeypatch):
    """某项持久化失败时，该项 internal_error，其他已落库项不受影响。"""
    from app import main as main_module

    real_persist = main_module._persist_audit
    calls = {"n": 0}

    def flaky_persist(conn, manifest, result, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_persist(conn, manifest, result, *args, **kwargs)

    monkeypatch.setattr(main_module, "_persist_audit", flaky_persist)

    payload = {
        "manifests": [
            {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
            {"dependencies": [{"name": "core-lib", "range": "^1.4.0"}]},
            {"dependencies": [{"name": "core-lib", "range": "^2.0.0"}]},
        ]
    }
    resp = client.post("/api/v1/audits/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["feasible"] == 2
    assert body["error"] == 1
    assert body["items"][1]["error"]["code"] == "internal_error"
    assert body["items"][0]["audit_id"]
    assert body["items"][2]["audit_id"]
    # 第 0、2 项的记录仍可独立查询
    assert client.get(f"/api/v1/audits/{body['items'][0]['audit_id']}").status_code == 200
    assert client.get(f"/api/v1/audits/{body['items'][2]['audit_id']}").status_code == 200


def test_single_audit_endpoint_still_works(client):
    """批量接口不改变原单条接口行为。"""
    resp = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "feasible"

    bad = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "x", "range": "nope"}]},
    )
    assert bad.status_code == 422
