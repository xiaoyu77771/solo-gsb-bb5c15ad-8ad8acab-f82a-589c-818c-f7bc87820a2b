"""历史影响反查 GET /api/v1/audits/impact/{package_name} 的端到端测试。"""


def _audit(client, **payload):
    resp = client.post("/api/v1/audits", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _impact(client, package, **params):
    resp = client.get(f"/api/v1/audits/impact/{package}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_unknown_package_returns_empty_not_404(client):
    _audit(
        client,
        dependencies=[{"name": "core-lib", "range": "^1.0.0"}],
    )
    resp = client.get("/api/v1/audits/impact/never-heard-of-it")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["versions"] == []
    assert body["summary"] == {
        "total": 0,
        "feasible": 0,
        "infeasible": 0,
        "inconclusive": 0,
        "resolution_hits": 0,
        "conflict_hits": 0,
        "root": 0,
        "direct": 0,
        "transitive": 0,
    }


def test_resolution_hit_feasible_with_transitive_depth(client):
    body = _audit(
        client,
        plugin_name="megaplugin",
        plugin_version="1.0.0",
        dependencies=[],
    )
    impact = _impact(client, "core-lib")
    assert impact["total"] == 1
    assert impact["summary"]["feasible"] == 1
    assert impact["summary"]["resolution_hits"] == 1
    assert impact["summary"]["conflict_hits"] == 0

    item = impact["items"][0]
    assert item["audit_id"] == body["id"]
    assert item["status"] == "feasible"
    assert item["resolution_match"] is True
    assert item["conflict_match"] is False
    # megaplugin -> authkit -> cachekit -> core-lib：传递依赖，最浅层为 2
    assert item["depth"] == 2
    assert item["direct"] is False
    assert item["as_root"] is False
    assert item["versions"] == ["1.5.0"]
    assert item["match_types"] == ["resolution"]

    resolved = next(m for m in item["matches"] if m["role"] == "resolved")
    assert resolved["version"] == "1.5.0"
    assert resolved["match_type"] == "resolution"
    assert resolved["depth"] == 2
    assert resolved["source_chain"][0]["package"] == "megaplugin"
    assert resolved["source_chain"][-1]["package"] in {"authkit", "httpkit"}


def test_root_plugin_hit_has_depth_zero(client):
    _audit(client, plugin_name="megaplugin", plugin_version="1.0.0", dependencies=[])
    item = _impact(client, "megaplugin")["items"][0]
    assert item["depth"] == 0
    assert item["as_root"] is True
    assert item["direct"] is False


def test_direct_dependency_depth_one_without_root(client):
    _audit(client, dependencies=[{"name": "authkit", "range": "^1.0.0"}])
    item = _impact(client, "authkit")["items"][0]
    assert item["depth"] == 1
    assert item["direct"] is True
    match = item["matches"][0]
    assert match["source_chain"][0]["package"] == "<manifest>"


def test_version_filter_exact_match(client):
    _audit(
        client,
        plugin_name="megaplugin",
        plugin_version="1.0.0",
        dependencies=[],
    )
    assert _impact(client, "core-lib", version="1.5.0")["total"] == 1
    # 同包其它版本不命中
    assert _impact(client, "core-lib", version="2.1.0")["total"] == 0
    # 未知版本形态：空结果而非 404
    assert _impact(client, "core-lib", version="9.9.9")["total"] == 0


def test_invalid_version_returns_422(client):
    resp = client.get("/api/v1/audits/impact/core-lib", params={"version": "nope"})
    assert resp.status_code == 422


def test_conflict_constrained_hit_selection_conflict(client):
    body = _audit(
        client,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    impact = _impact(client, "core-lib")
    assert impact["total"] == 1
    assert impact["summary"]["infeasible"] == 1
    assert impact["summary"]["resolution_hits"] == 0
    assert impact["summary"]["conflict_hits"] == 1

    item = impact["items"][0]
    assert item["audit_id"] == body["id"]
    assert item["resolution_match"] is False
    assert item["conflict_match"] is True
    assert "selection_conflict" in item["conflict_kinds"]

    constrained = [m for m in item["matches"] if m["role"] == "constrained"]
    assert constrained, "冲突对象必须作为 constrained 命中"
    assert all(m["match_type"] == "conflict" for m in constrained)
    assert {m["version"] for m in constrained} <= {"2.0.0", "2.1.0"}
    # cachekit/loggerkit 都是清单直接依赖 -> 经其一跳约束 core-lib，层级 2
    assert min(m["depth"] for m in constrained) == 2

    minimal = [m for m in constrained if m["minimal_chain"]]
    assert minimal, "最小冲突链中的约束必须带 minimal_chain=true"
    chain_packages = [hop["package"] for hop in minimal[0]["source_chain"]]
    assert chain_packages[0] == "<manifest>"
    assert chain_packages[-1] in {"cachekit", "loggerkit"}


def test_conflict_requester_hit_along_source_chain(client):
    # core-lib 是冲突对象；cachekit 出现在来源链上（cachekit -> core-lib）
    _audit(
        client,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    item = _impact(client, "cachekit")["items"][0]
    assert item["conflict_match"] is True
    requester = [m for m in item["matches"] if m["role"] == "requester"]
    assert requester
    assert all(m["match_type"] == "conflict" for m in requester)
    # cachekit 是清单直接依赖，作为链上根跳之后的第一跳 -> depth 1
    assert min(m["depth"] for m in requester) == 1
    assert "1.3.0" in item["versions"]


def test_conflict_version_filter_matches_locked_and_chain_hops(client):
    _audit(
        client,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    # selection_conflict 的 locked_version 命中
    assert _impact(client, "core-lib", version="2.0.0")["total"] == 1
    assert _impact(client, "core-lib", version="1.5.0")["total"] == 0
    # 来源链上 cachekit@1.3.0 参与了 core-lib 冲突
    assert _impact(client, "cachekit", version="1.3.0")["total"] == 1
    assert _impact(client, "cachekit", version="1.1.0")["total"] == 1


def test_range_conflict_hit_has_no_version_and_excluded_by_version_filter(client):
    _audit(
        client,
        dependencies=[
            {"name": "core-lib", "range": "^1.0.0"},
            {"name": "core-lib", "range": "^2.0.0"},
        ],
    )
    item = _impact(client, "core-lib")["items"][0]
    assert item["conflict_kinds"] == ["range_conflict"]
    assert item["depth"] == 1
    assert item["direct"] is True
    constrained = item["matches"]
    assert all(m["version"] is None for m in constrained)

    # range 冲突没有任何具体入选版本：指定版本反查时不命中
    assert _impact(client, "core-lib", version="1.0.0")["total"] == 0


def test_package_missing_hit(client):
    _audit(client, dependencies=[{"name": "ghostpkg", "range": "^1.0.0"}])
    item = _impact(client, "ghostpkg")["items"][0]
    assert item["conflict_kinds"] == ["package_missing"]
    assert item["depth"] == 1
    assert _impact(client, "ghostpkg", version="1.0.0")["total"] == 0


def test_status_filter_limits_scan(client):
    _audit(
        client,
        plugin_name="megaplugin",
        plugin_version="1.0.0",
        dependencies=[],
    )
    _audit(
        client,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    feasible = _impact(client, "core-lib", status="feasible")
    assert feasible["total"] == 1
    assert feasible["items"][0]["status"] == "feasible"

    infeasible = _impact(client, "core-lib", status="infeasible")
    assert infeasible["total"] == 1
    assert infeasible["items"][0]["status"] == "infeasible"

    assert _impact(client, "core-lib", status="inconclusive")["total"] == 0


def test_sorting_by_depth_then_created_at(client):
    # 先造一条传递命中 core-lib（depth 2，feasible）
    transitive = _audit(
        client,
        plugin_name="megaplugin",
        plugin_version="1.0.0",
        dependencies=[],
    )
    # 再造一条直接命中 core-lib（depth 1）——创建更晚但层级更浅，应排前面
    direct = _audit(client, dependencies=[{"name": "core-lib", "range": "~1.0.0"}])
    items = _impact(client, "core-lib")["items"]
    assert [it["audit_id"] for it in items] == [direct["id"], transitive["id"]]
    assert [it["depth"] for it in items] == [1, 2]


def test_limit_truncates_items_but_not_summary(client):
    # 三条均能解析到 core-lib 的可行审计（直接或传递）
    _audit(client, dependencies=[{"name": "core-lib", "range": "~1.0.0"}])
    _audit(client, dependencies=[{"name": "cachekit", "range": "^1.0.0"}])
    _audit(client, dependencies=[{"name": "httpkit", "range": "~2.2.0"}])
    impact = _impact(client, "core-lib", limit=1)
    assert impact["limit"] == 1
    assert impact["total"] == 3
    assert len(impact["items"]) == 1
    assert impact["summary"]["total"] == 3
    assert impact["summary"]["feasible"] == 3


def test_version_aggregation_when_version_omitted(client):
    _audit(
        client,
        plugin_name="megaplugin",
        plugin_version="1.0.0",
        dependencies=[],
    )
    _audit(
        client,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    impact = _impact(client, "core-lib")
    versions = {v["version"]: v for v in impact["versions"]}
    assert set(versions) == {"1.5.0", "2.0.0", "2.1.0"}
    assert versions["1.5.0"]["feasible"] == 1
    assert versions["2.0.0"]["infeasible"] == 1
    # 版本按 SemVer 排序（1.5.0 在 2.x 之前）
    assert [v["version"] for v in impact["versions"]] == ["1.5.0", "2.0.0", "2.1.0"]


def test_echoes_query_parameters(client):
    body = _impact(client, "core-lib", version="1.5.0", limit=5)
    assert body["package"] == "core-lib"
    assert body["version"] == "1.5.0"
    assert body["limit"] == 5
