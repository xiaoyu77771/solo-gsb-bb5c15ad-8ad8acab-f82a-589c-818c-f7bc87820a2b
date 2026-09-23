"""可验证审计导出包（audit JSONL bundle）的确定性构造与校验。

包格式（v1）：

- ``jsonl``：每行一条审计记录的规范化 JSON（键排序、无空白、UTF-8、ensure_ascii=False），
  以 ``\\n`` 结尾；记录顺序即调用方给定的顺序（按创建时间排序）。
- ``manifest``：清单，包含格式名/版本、``include_conflicts``、记录数、
  每条记录的 ``audit_id`` + ``sha256``（对该行字节的摘要）、整体 ``payload_sha256``
  （对整个 JSONL 字节串的摘要），以及对清单自身（除 ``manifest_sha256`` 外的全部字段）
  的摘要 ``manifest_sha256``。
- ``gzip_base64``：对包含 ``manifest`` 与 ``jsonl`` 的规范化信封 JSON 做
  gzip（mtime=0，确定性）后再 Base64 的结果。

任何字节或顺序改动都会在逐层校验中暴露（清单摘要 → 记录数 → 逐条解析/顺序/摘要/标志
→ 整体负载摘要）。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

BUNDLE_FORMAT = "audit-jsonl-bundle"
BUNDLE_FORMAT_VERSION = "1.0"

#: 校验通过 / 错误不针对具体记录时使用的下标
NO_INDEX = -1

#: 校验结果：(verified, first_error_index, error_type, error_detail)
VerifyResult = Tuple[bool, int, Optional[str], Optional[str]]


def canonical_json_bytes(obj: Any) -> bytes:
    """规范化 JSON 序列化：键排序、紧凑分隔、UTF-8、不转义非 ASCII。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _record_line_bytes(record: Dict[str, Any], include_conflicts: bool) -> bytes:
    if not include_conflicts:
        record = {k: v for k, v in record.items() if k != "conflicts"}
    return canonical_json_bytes(record)


def build_bundle(records: List[Dict[str, Any]], include_conflicts: bool) -> Dict[str, Any]:
    """根据已按目标顺序排好的审计记录构造导出包。

    ``records`` 中的元素应为 ``AuditResponse.model_dump(mode="json")`` 的结果。
    纯内存计算，不读写数据库。
    """
    lines: List[bytes] = []
    entries: List[Dict[str, str]] = []
    for record in records:
        line = _record_line_bytes(record, include_conflicts)
        lines.append(line)
        entries.append(
            {
                "audit_id": record["id"],
                "sha256": hashlib.sha256(line).hexdigest(),
            }
        )

    payload = b"".join(line + b"\n" for line in lines)
    manifest_body: Dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "include_conflicts": include_conflicts,
        "record_count": len(records),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "records": entries,
    }
    manifest_sha256 = hashlib.sha256(canonical_json_bytes(manifest_body)).hexdigest()
    manifest = {**manifest_body, "manifest_sha256": manifest_sha256}

    envelope = canonical_json_bytes(
        {
            "format": BUNDLE_FORMAT,
            "format_version": BUNDLE_FORMAT_VERSION,
            "manifest": manifest,
            "jsonl": payload.decode("utf-8"),
        }
    )
    gz = gzip.compress(envelope, compresslevel=9, mtime=0)

    return {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "include_conflicts": include_conflicts,
        "record_count": len(records),
        "jsonl": payload.decode("utf-8"),
        "records": entries,
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "gzip_base64": base64.b64encode(gz).decode("ascii"),
    }


def decode_bundle_base64(bundle_base64: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解码 Base64 + gunzip + 解析信封。

    成功返回 ``(envelope, None)``，失败返回 ``(None, error_type)``。
    """
    try:
        raw = base64.b64decode(bundle_base64, validate=True)
    except Exception:
        return None, "bundle_base64_invalid"
    try:
        data = gzip.decompress(raw)
    except Exception:
        return None, "bundle_gzip_invalid"
    try:
        envelope = json.loads(data.decode("utf-8"))
    except Exception:
        return None, "bundle_envelope_invalid"
    if not isinstance(envelope, dict) or "manifest" not in envelope or "jsonl" not in envelope:
        return None, "bundle_envelope_invalid"
    return envelope, None


def _fail(index: int, error_type: str, detail: str) -> VerifyResult:
    return False, index, error_type, detail


def verify_bundle(manifest: Any, jsonl_text: Any) -> VerifyResult:
    """校验导出包；纯内存计算，不读写数据库。

    校验顺序：清单结构 → 清单摘要 → 格式/版本 → JSONL 结构与记录数
    → 逐条（JSON 解析 → ID/顺序 → 行摘要 → conflicts 标志）→ 整体负载摘要。
    """
    # ---------- 清单结构与自身摘要（信任锚，最先校验） ----------
    if not isinstance(manifest, dict):
        return _fail(NO_INDEX, "malformed_manifest", "manifest 必须是 JSON 对象")
    required = {
        "format",
        "format_version",
        "include_conflicts",
        "record_count",
        "payload_sha256",
        "records",
        "manifest_sha256",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        return _fail(NO_INDEX, "malformed_manifest", f"manifest 缺少字段: {missing}")
    if not isinstance(manifest["records"], list) or not all(
        isinstance(e, dict)
        and isinstance(e.get("audit_id"), str)
        and isinstance(e.get("sha256"), str)
        for e in manifest["records"]
    ):
        return _fail(
            NO_INDEX,
            "malformed_manifest",
            "manifest.records 必须是由 audit_id/sha256 字符串组成的数组",
        )
    if not isinstance(manifest["include_conflicts"], bool):
        return _fail(NO_INDEX, "malformed_manifest", "manifest.include_conflicts 必须是布尔值")
    if not isinstance(manifest["record_count"], int) or isinstance(
        manifest["record_count"], bool
    ):
        return _fail(NO_INDEX, "malformed_manifest", "manifest.record_count 必须是整数")
    if not isinstance(manifest["payload_sha256"], str) or not isinstance(
        manifest["manifest_sha256"], str
    ):
        return _fail(NO_INDEX, "malformed_manifest", "清单摘要字段必须是字符串")

    expected_manifest_sha = manifest["manifest_sha256"]
    manifest_body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    actual_manifest_sha = hashlib.sha256(canonical_json_bytes(manifest_body)).hexdigest()
    if actual_manifest_sha != expected_manifest_sha:
        return _fail(
            NO_INDEX,
            "manifest_digest_mismatch",
            "manifest_sha256 与清单内容不一致，清单可能被改动",
        )

    if (
        manifest["format"] != BUNDLE_FORMAT
        or manifest["format_version"] != BUNDLE_FORMAT_VERSION
    ):
        return _fail(
            NO_INDEX,
            "unsupported_format",
            f"不支持的包格式: {manifest['format']} {manifest['format_version']}",
        )

    if not isinstance(jsonl_text, str):
        return _fail(NO_INDEX, "malformed_jsonl", "jsonl 必须是字符串")
    payload = jsonl_text.encode("utf-8")

    # ---------- JSONL 结构与记录数 ----------
    if not payload.endswith(b"\n"):
        return _fail(NO_INDEX, "malformed_jsonl", "JSONL 必须以换行符结尾")
    lines = payload[:-1].split(b"\n")
    if any(not line for line in lines):
        return _fail(NO_INDEX, "malformed_jsonl", "JSONL 中不允许出现空行")

    if len(lines) != manifest["record_count"]:
        return _fail(
            NO_INDEX,
            "record_count_mismatch",
            f"JSONL 记录数 {len(lines)} 与 manifest.record_count {manifest['record_count']} 不一致",
        )
    if len(lines) != len(manifest["records"]):
        return _fail(
            NO_INDEX,
            "record_count_mismatch",
            f"JSONL 记录数 {len(lines)} 与 manifest.records 条数 {len(manifest['records'])} 不一致",
        )

    # ---------- 逐条校验：解析 → 顺序/ID → 摘要 → conflicts 标志 ----------
    include_conflicts = manifest["include_conflicts"]
    for i, line in enumerate(lines):
        entry = manifest["records"][i]
        try:
            record = json.loads(line)
        except Exception as exc:
            return _fail(i, "record_parse_error", f"第 {i} 条记录不是合法 JSON: {exc}")
        if not isinstance(record, dict) or "id" not in record:
            return _fail(i, "record_id_missing", f"第 {i} 条记录缺少 id 字段")
        if record["id"] != entry["audit_id"]:
            return _fail(
                i,
                "record_order_mismatch",
                f"第 {i} 条记录 id={record['id']!r} 与清单顺序中的 "
                f"{entry['audit_id']!r} 不一致，记录顺序被改动",
            )
        actual_line_sha = hashlib.sha256(line).hexdigest()
        if actual_line_sha != entry["sha256"]:
            return _fail(
                i,
                "record_digest_mismatch",
                f"第 {i} 条记录 sha256 不一致，记录字节被改动",
            )
        has_conflicts = "conflicts" in record
        if include_conflicts and not has_conflicts:
            return _fail(
                i,
                "conflicts_flag_mismatch",
                f"第 {i} 条记录缺少 conflicts 字段，但 manifest.include_conflicts=true",
            )
        if not include_conflicts and has_conflicts:
            return _fail(
                i,
                "conflicts_flag_mismatch",
                f"第 {i} 条记录包含 conflicts 字段，但 manifest.include_conflicts=false",
            )

    # ---------- 整体负载摘要（换行等跨行字节改动在此兜底） ----------
    actual_payload_sha = hashlib.sha256(payload).hexdigest()
    if actual_payload_sha != manifest["payload_sha256"]:
        return _fail(
            NO_INDEX,
            "payload_digest_mismatch",
            "payload_sha256 与 JSONL 整体内容不一致，负载字节被改动",
        )

    return True, NO_INDEX, None, None
