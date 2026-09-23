"""API 端到端测试：健康检查、审计创建/查询、注册表、冲突链。"""


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["packages"] >= 6


def test_root_index(client):
    body = client.get("/").json()
    assert body["service"] == "plugin-dependency-audit"


def test_feasible_audit_persists_and_queries(client):
    payload = {
        "plugin_name": "megaplugin",
        "plugin_version": "1.0.0",
        "dependencies": [],
    }
    resp = client.post("/api/v1/audits", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "feasible"
    assert body["resolution"]["megaplugin"] == "1.0.0"
    assert body["resolution"]["authkit"].startswith("1.")
    assert body["conflicts"] == []
    audit_id = body["id"]

    # 逐条查询
    detail = client.get(f"/api/v1/audits/{audit_id}").json()
    assert detail["id"] == audit_id
    assert detail["resolution"]["core-lib"] == "1.5.0"
    # 每条选中的版本都带来源
    core_sources = next(s for s in detail["sources"] if s["package"] == "core-lib")
    assert core_sources["requested_by"], "必须记录每条约束的来源"

    # 列表查询
    listed = client.get("/api/v1/audits").json()
    assert listed["total"] >= 1
    assert any(item["id"] == audit_id for item in listed["items"])

    # 状态过滤
    infeasible_only = client.get("/api/v1/audits?status=infeasible").json()
    assert all(item["status"] == "infeasible" for item in infeasible_only["items"])


def test_infeasible_audit_reports_all_conflict_chains(client):
    payload = {
        "dependencies": [
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ]
    }
    resp = client.post("/api/v1/audits", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "infeasible"
    assert body["resolution"] is None
    assert len(body["conflicts"]) >= 2, "不能只报首个冲突，要给出多条本质不同的冲突链"

    conflict = body["conflicts"][0]
    assert conflict["package"] == "core-lib"
    minimal = conflict["minimal_constraints"]
    # 最小冲突链必须同时包含冲突两侧的约束及其逐跳来源
    ranges = sorted(c["version_range"] for c in minimal)
    assert any(r == "^2.0.0" for r in ranges)
    assert any(r.startswith("^1") or r.startswith(">=1") for r in ranges)
    for c in minimal:
        assert c["source_chain"], "每条约束必须带 source_chain"
        first_hop = c["source_chain"][0]
        assert first_hop["package"] == "<manifest>"

    # 审计记录可查询，状态一致
    detail = client.get(f"/api/v1/audits/{body['id']}").json()
    assert detail["status"] == "infeasible"
    assert len(detail["conflicts"]) == len(body["conflicts"])


def test_range_conflict_direct_contradiction(client):
    payload = {
        "dependencies": [
            {"name": "core-lib", "range": "^1.0.0"},
            {"name": "core-lib", "range": "^2.0.0"},
        ]
    }
    body = client.post("/api/v1/audits", json=payload).json()
    assert body["status"] == "infeasible"
    kinds = {c["kind"] for c in body["conflicts"]}
    assert "range_conflict" in kinds
    conflict = next(c for c in body["conflicts"] if c["kind"] == "range_conflict")
    assert len(conflict["minimal_constraints"]) == 2


def test_missing_package(client):
    body = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "does-not-exist", "range": "^1.0.0"}]},
    ).json()
    assert body["status"] == "infeasible"
    assert body["conflicts"][0]["kind"] == "package_missing"


def test_invalid_range_rejected(client):
    resp = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "x", "range": "not-valid"}]},
    )
    assert resp.status_code == 422


def test_unknown_audit_404(client):
    assert client.get("/api/v1/audits/nope").status_code == 404


def test_registry_write_then_resolve(client):
    resp = client.put(
        "/api/v1/registry/packages/newlib",
        json={
            "name": "newlib",
            "versions": [
                {"version": "1.0.0", "dependencies": {"core-lib": "^2.0.0"}},
            ],
        },
    )
    assert resp.status_code == 200
    packages = client.get("/api/v1/registry/packages").json()["packages"]
    assert "newlib" in packages

    body = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "newlib", "range": "^1.0.0"}]},
    ).json()
    assert body["status"] == "feasible"
    assert body["resolution"]["core-lib"] == "2.1.0"


def test_prerelease_not_included_by_default(client):
    body = client.post(
        "/api/v1/audits",
        json={"dependencies": [{"name": "core-lib", "range": "~1.0.0"}]},
    ).json()
    assert body["status"] == "feasible"
    assert body["resolution"]["core-lib"] == "1.0.0"  # 而不是 1.0.0-rc.1
