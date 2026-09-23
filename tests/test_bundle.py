"""可验证导出包测试：确定性、错误码、逐字节/顺序篡改识别。"""

import base64
import gzip
import hashlib
import json

from app.bundle import build_bundle, verify_bundle


def _create_audit(client, **overrides):
    payload = {
        "plugin_name": "megaplugin",
        "plugin_version": "1.0.0",
        "dependencies": [],
    }
    payload.update(overrides)
    resp = client.post("/api/v1/audits", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _export(client, audit_ids, include_conflicts=False):
    resp = client.post(
        "/api/v1/audits/export-bundle",
        json={"audit_ids": audit_ids, "include_conflicts": include_conflicts},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _verify_b64(client, b64):
    return client.post("/api/v1/audits/verify-bundle", json={"gzip_base64": b64}).json()


# ---------- 导出基础 ----------

def test_export_bundle_deterministic_and_verified(client):
    a = _create_audit(client)
    b = _create_audit(
        client,
        plugin_name=None,
        plugin_version=None,
        dependencies=[{"name": "cachekit", "range": "^1.0.0"}],
    )
    ids = [a["id"], b["id"]]

    body = _export(client, ids, include_conflicts=True)
    assert body["format"] == "audit-jsonl-bundle"
    assert body["format_version"] == "1.0"
    assert body["record_count"] == 2
    assert body["include_conflicts"] is True

    # JSONL：每行一条、以 \n 结尾、行内 JSON 键有序且紧凑
    lines = body["jsonl"].splitlines()
    assert len(lines) == 2
    assert body["jsonl"].endswith("\n")
    assert json.loads(lines[0])["id"] == a["id"]
    for i, line in enumerate(lines):
        assert body["records"][i]["audit_id"] == ids[i]
        assert body["records"][i]["sha256"] == hashlib.sha256(
            line.encode("utf-8")
        ).hexdigest()

    # manifest 摘要、整体负载摘要
    manifest = body["manifest"]
    assert manifest["payload_sha256"] == hashlib.sha256(
        body["jsonl"].encode("utf-8")
    ).hexdigest()
    manifest_body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    canonical = json.dumps(
        manifest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert body["manifest_sha256"] == hashlib.sha256(canonical).hexdigest()

    # gzip_base64 可解码回含 manifest/jsonl 的信封
    envelope = json.loads(gzip.decompress(base64.b64decode(body["gzip_base64"])))
    assert envelope["manifest"] == manifest
    assert envelope["jsonl"] == body["jsonl"]

    # 校验通过
    result = _verify_b64(client, body["gzip_base64"])
    assert result == {
        "verified": True,
        "first_error_index": -1,
        "error_type": None,
        "error_detail": None,
    }

    # 确定性：同样输入两次导出，字节完全一致（gzip mtime=0）
    body2 = _export(client, ids, include_conflicts=True)
    assert body2["jsonl"] == body["jsonl"]
    assert body2["gzip_base64"] == body["gzip_base64"]
    assert body2["manifest_sha256"] == body["manifest_sha256"]


def test_export_without_conflicts_omits_field(client):
    infeasible = _create_audit(
        client,
        plugin_name=None,
        plugin_version=None,
        dependencies=[
            {"name": "cachekit", "range": "^1.0.0"},
            {"name": "loggerkit", "range": "^0.9.0"},
        ],
    )
    body = _export(client, [infeasible["id"]], include_conflicts=False)
    line = json.loads(body["jsonl"])
    assert "conflicts" not in line
    assert _verify_b64(client, body["gzip_base64"])["verified"] is True

    body_inc = _export(client, [infeasible["id"]], include_conflicts=True)
    assert "conflicts" in body_inc["jsonl"]
    assert _verify_b64(client, body_inc["gzip_base64"])["verified"] is True


def test_export_unknown_id_404(client):
    resp = client.post(
        "/api/v1/audits/export-bundle",
        json={"audit_ids": ["does-not-exist"]},
    )
    assert resp.status_code == 404


def test_export_duplicate_ids_422(client):
    a = _create_audit(client)
    resp = client.post(
        "/api/v1/audits/export-bundle",
        json={"audit_ids": [a["id"], a["id"]]},
    )
    assert resp.status_code == 422


def test_export_id_count_limits_422(client):
    resp = client.post("/api/v1/audits/export-bundle", json={"audit_ids": []})
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/audits/export-bundle",
        json={"audit_ids": ["x"] * 101},
    )
    assert resp.status_code == 422


def test_export_and_verify_do_not_write(client):
    before = client.get("/api/v1/audits").json()["total"]
    a = _create_audit(client)
    body = _export(client, [a["id"]])
    _verify_b64(client, body["gzip_base64"])
    client.post(
        "/api/v1/audits/verify-bundle",
        json={"jsonl": body["jsonl"], "manifest": body["manifest"]},
    )
    after = client.get("/api/v1/audits").json()["total"]
    assert after == before + 1  # 只有创建审计那一次写入


# ---------- 篡改识别 ----------

def _envelope(client, ids, include_conflicts=True):
    body = _export(client, ids, include_conflicts=include_conflicts)
    return body, json.loads(gzip.decompress(base64.b64decode(body["gzip_base64"])))


def _repack(envelope):
    raw = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")


def test_verify_record_order_swap_detected(client):
    a = _create_audit(client)
    b = _create_audit(
        client, plugin_name=None, plugin_version=None,
        dependencies=[{"name": "cachekit", "range": "^1.0.0"}],
    )
    _, envelope = _envelope(client, [a["id"], b["id"]])

    lines = envelope["jsonl"].splitlines()
    envelope["jsonl"] = "\n".join(reversed(lines)) + "\n"
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["first_error_index"] == 0
    assert result["error_type"] == "record_order_mismatch"


def test_verify_single_byte_change_detected(client):
    a = _create_audit(client)
    _, envelope = _envelope(client, [a["id"]])

    line = envelope["jsonl"].rstrip("\n").encode("utf-8")
    # 翻转一个仍处于合法 JSON 区域的字节（避开引号与花括号：把 node_count 数字改写）
    decoded = json.loads(line)
    tampered = dict(decoded)
    tampered["node_count"] = decoded["node_count"] + 1
    tampered_line = json.dumps(
        tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    envelope["jsonl"] = tampered_line.decode("utf-8") + "\n"
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["first_error_index"] == 0
    assert result["error_type"] == "record_digest_mismatch"


def test_verify_json_parse_error_detected(client):
    a = _create_audit(client)
    _, envelope = _envelope(client, [a["id"]])
    envelope["jsonl"] = "{not-json}\n"
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["error_type"] == "record_parse_error"
    assert result["first_error_index"] == 0


def test_verify_record_count_mismatch_detected(client):
    a = _create_audit(client)
    _, envelope = _envelope(client, [a["id"]])
    envelope["manifest"]["record_count"] = 2
    envelope["manifest"]["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in envelope["manifest"].items() if k != "manifest_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["error_type"] == "record_count_mismatch"
    assert result["first_error_index"] == -1


def test_verify_manifest_tampering_detected(client):
    a = _create_audit(client)
    _, envelope = _envelope(client, [a["id"]])
    envelope["manifest"]["record_count"] = 2  # 不重算 manifest_sha256
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["error_type"] == "manifest_digest_mismatch"


def test_verify_conflicts_flag_mismatch_detected(client):
    # 行与逐条摘要完全匹配，仅翻转 include_conflicts 并重算清单摘要，
    # 从而保证错误落在标志校验而非摘要校验上。
    a = _create_audit(
        client, plugin_name=None, plugin_version=None,
        dependencies=[{"name": "cachekit", "range": "^1.0.0"}],
    )
    body_false = _export(client, [a["id"]], include_conflicts=False)
    manifest = dict(body_false["manifest"])
    manifest["include_conflicts"] = True
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in manifest.items() if k != "manifest_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    result = client.post(
        "/api/v1/audits/verify-bundle",
        json={"jsonl": body_false["jsonl"], "manifest": manifest},
    ).json()
    assert result["verified"] is False
    assert result["error_type"] == "conflicts_flag_mismatch"
    assert result["first_error_index"] == 0


def test_verify_first_error_index_points_to_later_record(client):
    a = _create_audit(client)
    b = _create_audit(
        client, plugin_name=None, plugin_version=None,
        dependencies=[{"name": "cachekit", "range": "^1.0.0"}],
    )
    _, envelope = _envelope(client, [a["id"], b["id"]])
    lines = envelope["jsonl"].splitlines()
    record = json.loads(lines[1])
    record["explanation"] = record["explanation"] + " tampered"
    lines[1] = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    envelope["jsonl"] = "\n".join(lines) + "\n"
    result = _verify_b64(client, _repack(envelope))
    assert result["verified"] is False
    assert result["first_error_index"] == 1
    assert result["error_type"] == "record_digest_mismatch"


def test_verify_invalid_base64_and_gzip(client):
    result = client.post(
        "/api/v1/audits/verify-bundle",
        json={"gzip_base64": "!!!not-base64!!!"},
    ).json()
    assert result["verified"] is False
    assert result["error_type"] == "bundle_base64_invalid"

    result = client.post(
        "/api/v1/audits/verify-bundle",
        json={"gzip_base64": base64.b64encode(b"not gzip data").decode()},
    ).json()
    assert result["error_type"] == "bundle_gzip_invalid"

    result = client.post(
        "/api/v1/audits/verify-bundle",
        json={"gzip_base64": base64.b64encode(gzip.compress(b"{}")).decode()},
    ).json()
    assert result["error_type"] == "bundle_envelope_invalid"


def test_verify_requires_payload(client):
    resp = client.post("/api/v1/audits/verify-bundle", json={})
    assert resp.status_code == 422
    resp = client.post("/api/v1/audits/verify-bundle", json={"jsonl": "x\n"})
    assert resp.status_code == 422


# ---------- 纯函数级别 ----------

def test_build_bundle_unit_roundtrip():
    records = [
        {"id": "a", "node_count": 1, "conflicts": []},
        {"id": "b", "node_count": 2, "conflicts": []},
    ]
    bundle = build_bundle(records, include_conflicts=False)
    verified, index, error_type, _ = verify_bundle(
        bundle["manifest"], bundle["jsonl"]
    )
    assert verified is True
    assert index == -1
    assert error_type is None
