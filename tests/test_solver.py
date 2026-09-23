"""求解器单元测试：传递解析、完整搜索、冲突链与来源。"""

import json
import os

import pytest

from app.solver import DEFAULT_NODE_BUDGET, Solver


@pytest.fixture(scope="module")
def registry():
    path = os.path.join(os.path.dirname(__file__), "..", "data", "seed_registry.json")
    raw = json.load(open(path, encoding="utf-8"))
    return {n: {v["version"]: v["dependencies"] for v in p["versions"]} for n, p in raw.items()}


def test_resolves_transitive_and_picks_highest(registry):
    r = Solver(registry).solve([("authkit", "^1.0.0")])
    assert r.status == "feasible"
    assert r.resolution == {
        "authkit": "1.2.0",
        "cachekit": "1.3.0",
        "core-lib": "1.5.0",
    }


def test_root_plugin_dependencies_expanded(registry):
    r = Solver(registry).solve([], root_name="megaplugin", root_version="1.0.0")
    assert r.status == "feasible"
    assert set(r.resolution) == {
        "megaplugin", "authkit", "httpkit", "cachekit", "loggerkit", "core-lib",
    }


def test_direct_range_contradiction_is_minimal(registry):
    r = Solver(registry).solve([("core-lib", "^1.0.0"), ("core-lib", "^2.0.0")])
    assert r.status == "infeasible"
    assert len(r.conflicts) == 1
    c = r.conflicts[0]
    assert c["kind"] == "range_conflict"
    assert len(c["minimal_constraints"]) == 2
    # all_constraints >= minimal
    assert len(c["all_constraints"]) == 2


def test_search_does_not_stop_at_first_conflict(registry):
    """authkit2 -> cachekit* -> core-lib^1 与 httpkit3 -> loggerkit -> core-lib^2 冲突；
    cachekit 三个版本、core-lib 两个锁定版本都要被枚举到。"""
    r = Solver(registry).solve([("httpkit", "^3.0.0"), ("authkit", "^2.0.0")])
    assert r.status == "infeasible"
    assert len(r.conflicts) >= 2, "必须继续搜索其它分支，不能只报首个冲突"
    # 每条最小冲突链必须同时含 ^1 侧与 ^2 侧
    for c in r.conflicts:
        ranges = [m["version_range"] for m in c["minimal_constraints"]]
        assert any("2" in rng for rng in ranges)
        assert any(rng.startswith("^1") or rng.startswith(">=1") for rng in ranges)
        # 来源链逐跳可达 manifest
        for m in c["minimal_constraints"]:
            assert m["source_chain"][0]["package"] == "<manifest>"
            # 传递深度 >= 2 的约束保留完整路径
            if m["package"] == "core-lib" and "cachekit" in str(m["source_chain"]):
                pkgs = [s["package"] for s in m["source_chain"]]
                assert "authkit" in pkgs and "cachekit" in pkgs


def test_missing_package(registry):
    r = Solver(registry).solve([("ghost", "^1.0.0")])
    assert r.status == "infeasible"
    assert r.conflicts[0]["kind"] == "package_missing"


def test_deep_transitive_chain_reports_full_path(registry):
    r = Solver(registry).solve([("megaplugin", "^1.0.0"), ("loggerkit", "~0.4.0")])
    # megaplugin -> httpkit2.2 -> loggerkit >=0.5 <0.9，与根的 ~0.4.0 冲突
    assert r.status == "infeasible"
    # 至少有一条冲突链能体现 megaplugin -> httpkit -> loggerkit 的传递路径
    deep = False
    for c in r.conflicts:
        for m in c["minimal_constraints"]:
            pkgs = [s["package"] for s in m["source_chain"]]
            if "megaplugin" in pkgs and "httpkit" in pkgs:
                deep = True
    assert deep


def test_node_budget_protection(registry):
    r = Solver(registry, node_budget=0).solve([("authkit", "^1.0.0")])
    assert r.status == "inconclusive"
    assert r.node_count > DEFAULT_NODE_BUDGET or r.node_count >= 1
