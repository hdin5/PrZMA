from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from evaluation.cache_recovery.chromecacheview import CacheRecord, normalize_media_type


def _snapshot_valid(manifest: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping)
        and int(item.get("size") or 0) > 0
        and isinstance(item.get("meta"), Mapping)
        and item["meta"].get("artifact_key") == "chromium.cache.cache_data"
        for item in manifest.get("artifacts") or []
    )


def _file_hashes(root: Path) -> Counter[str]:
    hashes: Counter[str] = Counter()
    if not Path(root).is_dir():
        return hashes
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[digest.hexdigest()] += 1
    return hashes


def _normalize_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return str(url or "").strip()
    default_port = 80 if scheme == "http" else 443
    port = parsed.port or default_port
    netloc = hostname if port == default_port else f"{hostname}:{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def _full_url(base_origin: str, value: str) -> str:
    return _normalize_url(urljoin(base_origin.rstrip("/") + "/", str(value or "").lstrip("/")))


def _size_for(item: Mapping[str, Any], semantics: Mapping[str, str]) -> int:
    encoding = str(item.get("content_encoding") or "identity").lower()
    mode = semantics.get(encoding, semantics.get("identity", "decoded"))
    return int(item.get("encoded_size" if mode == "encoded" else "decoded_size") or 0)


def _hash_for(item: Mapping[str, Any], semantics: Mapping[str, str]) -> str:
    encoding = str(item.get("content_encoding") or "identity").lower()
    mode = semantics.get(encoding, semantics.get("identity", "decoded"))
    return str(item.get("encoded_sha256" if mode == "encoded" else "decoded_sha256") or "")


def _rate(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(1 for row in rows if row.get(field) is True) / len(rows) if rows else 0.0


def _status_matches(observed: str, expected: str) -> bool:
    return bool(re.search(rf"\b{re.escape(str(expected).strip())}\b", str(observed or "")))


def compare_autonomous_run(
    resource_manifest: Mapping[str, Any],
    behavior_manifest: Mapping[str, Any],
    requests: Iterable[Mapping[str, Any]],
    snapshot_manifest: Mapping[str, Any],
    records: Sequence[CacheRecord],
    extracted_dir: Path,
    *,
    base_origin: str,
    size_semantics: Mapping[str, str] | None = None,
    content_semantics: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    size_modes = dict(
        size_semantics
        or {"identity": "decoded", "gzip": "encoded", "deflate": "encoded"}
    )
    content_modes = dict(
        content_semantics
        or {"identity": "decoded", "gzip": "decoded", "deflate": "encoded"}
    )
    catalog = [
        item
        for item in resource_manifest.get("resources") or []
        if isinstance(item, Mapping) and item.get("cache_eligible") is True
    ]
    request_events = [event for event in requests if isinstance(event, Mapping)]
    generated_keys = {
        (str(event.get("resource_id") or ""), str(event.get("variant_id") or "default"))
        for event in request_events
    }
    generated_resource_ids = {
        str(event.get("resource_id") or "") for event in request_events
    }
    snapshot_ok = _snapshot_valid(snapshot_manifest)
    extracted_hashes = _file_hashes(Path(extracted_dir))
    by_url: Dict[str, List[CacheRecord]] = defaultdict(list)
    for record in records:
        by_url[_normalize_url(record.url)].append(record)

    rows: List[Dict[str, Any]] = []
    canonical_urls = set()
    for item in catalog:
        resource_id = str(item.get("resource_id") or "")
        variant_id = str(item.get("variant_id") or "default")
        expected_url = _full_url(base_origin, str(item.get("url") or ""))
        canonical_urls.add(expected_url)
        candidates = by_url.get(expected_url, [])
        expected_mime = normalize_media_type(str(item.get("media_type") or ""))
        expected_size = _size_for(item, size_modes)
        expected_hash = _hash_for(item, content_modes)
        expected_encoding = str(item.get("content_encoding") or "identity").lower()
        expected_status = str(int(item.get("status") or 0))

        def candidate_score(record: CacheRecord) -> int:
            observed_encoding = record.content_encoding or "identity"
            return sum(
                (
                    normalize_media_type(record.content_type) == expected_mime,
                    record.file_size == expected_size,
                    observed_encoding == expected_encoding,
                    _status_matches(record.server_response, expected_status),
                )
            )

        record = max(candidates, key=candidate_score) if candidates else None
        generated = (resource_id, variant_id) in generated_keys
        recovered = record is not None
        mime_ok = recovered and normalize_media_type(record.content_type) == expected_mime
        size_ok = recovered and record.file_size == expected_size
        observed_encoding = (record.content_encoding or "identity") if record else None
        encoding_ok = recovered and observed_encoding == expected_encoding
        status_ok = recovered and _status_matches(record.server_response, expected_status)
        content_ok = recovered and bool(expected_hash) and extracted_hashes[expected_hash] > 0
        e2e_matched = generated and snapshot_ok and recovered and content_ok

        failure = None
        if not generated:
            failure = "NOT_GENERATED"
        elif not snapshot_ok:
            failure = "ARTIFACT_COLLECTION_FAILED"
        elif not recovered:
            failure = "RECOVERY_MISS"
        elif not content_ok:
            failure = "CONTENT_HASH_MISMATCH"

        rows.append(
            {
                "resource_id": resource_id,
                "variant_id": variant_id,
                "classification": item.get("classification"),
                "journey": item.get("journey"),
                "trigger": item.get("trigger"),
                "url": expected_url,
                "generated": generated,
                "snapshot_present": snapshot_ok,
                "tool_record_present": recovered,
                "url_correct": recovered,
                "media_type_correct": mime_ok,
                "size_correct": size_ok,
                "content_encoding_correct": encoding_ok,
                "server_status_correct": status_ok,
                "content_hash_correct": content_ok,
                "e2e_matched": e2e_matched,
                "failure_code": failure,
                "expected_media_type": expected_mime,
                "observed_media_type": record.content_type if record else None,
                "expected_size": expected_size,
                "observed_size": record.file_size if record else None,
                "expected_content_encoding": expected_encoding,
                "observed_content_encoding": observed_encoding,
                "duplicate_record_count": max(0, len(candidates) - 1),
            }
        )

    canonical_interactive = [row for row in rows if row["classification"] == "interactive"]
    canonical_passive = [row for row in rows if row["classification"] == "passive"]
    canonical_matched_interactive = [
        row for row in canonical_interactive if row["e2e_matched"]
    ]

    catalog_by_key = {
        (str(item.get("resource_id") or ""), str(item.get("variant_id") or "default")): item
        for item in catalog
    }
    catalog_by_resource: Dict[str, Mapping[str, Any]] = {}
    for item in catalog:
        catalog_by_resource.setdefault(str(item.get("resource_id") or ""), item)

    runtime_rows: List[Dict[str, Any]] = []
    runtime_urls = set()
    seen_runtime = set()
    for event in request_events:
        resource_id = str(event.get("resource_id") or "")
        variant_id = str(event.get("variant_id") or "default")
        fallback = catalog_by_key.get((resource_id, variant_id)) or catalog_by_resource.get(
            resource_id
        )
        if not fallback:
            continue
        expected = dict(fallback)
        expected.update(event)
        expected_url = _normalize_url(
            str(event.get("request_url") or "")
            or _full_url(base_origin, str(fallback.get("url") or ""))
        )
        runtime_key = (resource_id, expected_url)
        if runtime_key in seen_runtime:
            continue
        seen_runtime.add(runtime_key)
        runtime_urls.add(expected_url)
        candidates = by_url.get(expected_url, [])
        expected_mime = normalize_media_type(str(expected.get("media_type") or ""))
        expected_size = _size_for(expected, size_modes)
        expected_hash = _hash_for(expected, content_modes)
        expected_encoding = str(expected.get("content_encoding") or "identity").lower()
        expected_status = str(int(expected.get("status") or 0))

        def runtime_candidate_score(record: CacheRecord) -> int:
            observed_encoding = record.content_encoding or "identity"
            return sum(
                (
                    normalize_media_type(record.content_type) == expected_mime,
                    record.file_size == expected_size,
                    observed_encoding == expected_encoding,
                    _status_matches(record.server_response, expected_status),
                )
            )

        record = max(candidates, key=runtime_candidate_score) if candidates else None
        recovered = record is not None
        observed_encoding = (record.content_encoding or "identity") if record else None
        content_ok = recovered and bool(expected_hash) and extracted_hashes[expected_hash] > 0
        e2e_matched = snapshot_ok and recovered and content_ok
        failure = None
        if not snapshot_ok:
            failure = "ARTIFACT_COLLECTION_FAILED"
        elif not recovered:
            failure = "RECOVERY_MISS"
        elif not content_ok:
            failure = "CONTENT_HASH_MISMATCH"
        runtime_rows.append(
            {
                "resource_id": resource_id,
                "variant_id": variant_id,
                "classification": expected.get("classification"),
                "journey": expected.get("journey"),
                "trigger": expected.get("trigger"),
                "url": expected_url,
                "parameters": dict(event.get("parameters") or {}),
                "request_sequence": event.get("request_sequence"),
                "generated": True,
                "snapshot_present": snapshot_ok,
                "tool_record_present": recovered,
                "url_correct": recovered,
                "media_type_correct": (
                    recovered and normalize_media_type(record.content_type) == expected_mime
                ),
                "size_correct": recovered and record.file_size == expected_size,
                "content_encoding_correct": recovered and observed_encoding == expected_encoding,
                "server_status_correct": (
                    recovered and _status_matches(record.server_response, expected_status)
                ),
                "content_hash_correct": content_ok,
                "e2e_matched": e2e_matched,
                "failure_code": failure,
                "expected_media_type": expected_mime,
                "observed_media_type": record.content_type if record else None,
                "expected_size": expected_size,
                "observed_size": record.file_size if record else None,
                "expected_content_encoding": expected_encoding,
                "observed_content_encoding": observed_encoding,
                "duplicate_record_count": max(0, len(candidates) - 1),
            }
        )

    runtime_interactive = [
        row for row in runtime_rows if row["classification"] == "interactive"
    ]
    recovered_runtime_interactive = [
        row for row in runtime_interactive if row["tool_record_present"]
    ]
    matched_runtime_interactive = [
        row for row in runtime_interactive if row["e2e_matched"]
    ]
    interactive_family_ids = {
        str(item.get("resource_id") or "")
        for item in catalog
        if item.get("classification") == "interactive"
    }
    generated_family_ids = {row["resource_id"] for row in runtime_interactive}
    recovered_family_ids = {
        row["resource_id"] for row in recovered_runtime_interactive
    }
    matched_family_ids = {row["resource_id"] for row in matched_runtime_interactive}

    behaviors = [
        behavior
        for behavior in behavior_manifest.get("behaviors") or []
        if isinstance(behavior, Mapping)
    ]
    behavior_rows = []
    for behavior in behaviors:
        evidence_ids = {
            str(value) for value in behavior.get("evidence_resource_ids") or []
        }
        verified = bool(evidence_ids) and evidence_ids.issubset(generated_resource_ids)
        behavior_rows.append(
            {
                "behavior_id": str(behavior.get("behavior_id") or ""),
                "journey": behavior.get("journey"),
                "effect": behavior.get("effect"),
                "verified": verified,
                "evidence_resource_ids": sorted(evidence_ids),
            }
        )

    normalized_origin = _normalize_url(base_origin.rstrip("/") + "/")
    origin_parts = urlsplit(normalized_origin)
    target_records = [
        record
        for record in records
        if (
            urlsplit(_normalize_url(record.url)).scheme,
            urlsplit(_normalize_url(record.url)).netloc,
        )
        == (origin_parts.scheme, origin_parts.netloc)
    ]
    matched_target_records = sum(
        1 for record in target_records if _normalize_url(record.url) in runtime_urls
    )

    runtime_parameter_variants = {
        (row["resource_id"], json.dumps(row["parameters"], sort_keys=True, separators=(",", ":")))
        for row in runtime_interactive
        if row.get("parameters")
    }

    summary = {
        "interactive_target_count": len(interactive_family_ids),
        "passive_target_count": len(canonical_passive),
        "generated_interactive_count": len(generated_family_ids),
        "recovered_interactive_count": len(recovered_family_ids),
        "e2e_matched_interactive_count": len(matched_family_ids),
        "autonomous_e2e_artifact_coverage": (
            len(matched_family_ids) / len(interactive_family_ids)
            if interactive_family_ids
            else 0.0
        ),
        "generation_coverage": (
            len(generated_family_ids) / len(interactive_family_ids)
            if interactive_family_ids
            else 0.0
        ),
        "recovery_recall": (
            len(matched_runtime_interactive) / len(runtime_interactive)
            if runtime_interactive
            else 0.0
        ),
        "runtime_generated_interactive_instance_count": len(runtime_interactive),
        "runtime_recovered_interactive_instance_count": len(recovered_runtime_interactive),
        "runtime_e2e_matched_interactive_instance_count": len(matched_runtime_interactive),
        "canonical_interactive_variant_count": len(canonical_interactive),
        "canonical_e2e_matched_variant_count": len(canonical_matched_interactive),
        "canonical_variant_coverage": (
            len(canonical_matched_interactive) / len(canonical_interactive)
            if canonical_interactive
            else 0.0
        ),
        "distinct_runtime_parameter_variants": len(runtime_parameter_variants),
        "behavior_count": len(behavior_rows),
        "verified_behavior_count": sum(1 for row in behavior_rows if row["verified"]),
        "behavior_coverage": (
            sum(1 for row in behavior_rows if row["verified"]) / len(behavior_rows)
            if behavior_rows
            else 0.0
        ),
        "parsing_precision": (
            matched_target_records / len(target_records) if target_records else 0.0
        ),
        "target_domain_record_count": len(target_records),
        "matched_target_domain_record_count": matched_target_records,
        "snapshot_present": snapshot_ok,
        "field_accuracy_denominator": len(recovered_runtime_interactive),
        "field_accuracy": {
            "url": _rate(recovered_runtime_interactive, "url_correct"),
            "media_type": _rate(recovered_runtime_interactive, "media_type_correct"),
            "size": _rate(recovered_runtime_interactive, "size_correct"),
            "content_encoding": _rate(
                recovered_runtime_interactive, "content_encoding_correct"
            ),
            "server_status": _rate(recovered_runtime_interactive, "server_status_correct"),
            "content_identity": _rate(
                recovered_runtime_interactive, "content_hash_correct"
            ),
        },
        "size_semantics": size_modes,
        "content_semantics": content_modes,
    }
    return {
        "schema_version": "1.0.0",
        "summary": summary,
        "behaviors": behavior_rows,
        "resources": rows,
        "runtime_resources": runtime_rows,
    }
