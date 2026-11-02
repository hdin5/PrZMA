from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from evaluation.cache_recovery.autonomous_comparator import compare_autonomous_run
from evaluation.cache_recovery.chromecacheview import parse_cache_csv, run_chromecacheview


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _read_jsonl(path: Path) -> list[Dict[str, Any]]:
    values = []
    if not Path(path).is_file():
        return values
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def select_agent_executions(
    actions: Iterable[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    return [
        dict(entry)
        for entry in actions
        if str(entry.get("kind") or "") == "action"
        and str(entry.get("origin") or "") == "agent"
        and isinstance(entry.get("action"), Mapping)
        and (entry.get("action") or {}).get("name")
        and not (
            isinstance(entry.get("result"), Mapping)
            and (entry.get("result") or {}).get("skipped") is True
        )
    ]


def replay_agent_actions(
    actions: Sequence[Mapping[str, Any]],
    *,
    replay_run_id: str,
    execute: Callable[[Dict[str, Any]], Mapping[str, Any]],
    sleep: Callable[[float], None] = time.sleep,
) -> list[Dict[str, Any]]:
    results = []
    previous_timestamp: datetime | None = None
    selected_actions = select_agent_executions(actions)
    for index, entry in enumerate(selected_actions, start=1):
        try:
            source_timestamp = datetime.fromisoformat(str(entry.get("ts") or ""))
        except ValueError:
            source_timestamp = None
        if previous_timestamp is not None and source_timestamp is not None:
            delay = (source_timestamp - previous_timestamp).total_seconds()
            if delay > 0:
                sleep(delay)
        previous_timestamp = source_timestamp
        action = entry["action"]
        request = {
            "schema_version": "1.0.0",
            "run_id": replay_run_id,
            "agent_id": str(entry.get("agent_id") or "A1"),
            "action_id": f"replay_{index:04d}",
            "name": str(action.get("name")),
            "params": dict(action.get("params") or {}),
        }
        result = dict(execute(request))
        results.append(
            {
                "schema_version": "1.0.0",
                "replay_type": "action",
                "replay_index": index,
                "source_run_id": str(entry.get("run_id") or ""),
                "request": request,
                "result": result,
                "excluded_from_campaign_metrics": True,
            }
        )
    cleanup_timestamps = []
    for entry in actions:
        action = entry.get("action") or {}
        if (
            str(entry.get("origin") or "") != "system_cleanup"
            or str(action.get("name") or "") != "browser.close"
        ):
            continue
        try:
            cleanup_timestamps.append(datetime.fromisoformat(str(entry.get("ts") or "")))
        except ValueError:
            continue
    if previous_timestamp is not None:
        later_cleanup = [value for value in cleanup_timestamps if value > previous_timestamp]
        if later_cleanup:
            sleep((min(later_cleanup) - previous_timestamp).total_seconds())
    return results


def _normalize_run_scoped_action_params(
    action_name: str,
    params: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = deepcopy(dict(params))
    if action_name != "browser.goto" or not isinstance(normalized.get("url"), str):
        return normalized
    parsed = urlsplit(normalized["url"])
    if parsed.path.rstrip("/") != "/start":
        return normalized
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "run_id" for key, _value in query):
        return normalized
    normalized_query = [
        (key, "<run_id>" if key == "run_id" else value) for key, value in query
    ]
    normalized["url"] = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(normalized_query), parsed.fragment)
    )
    return normalized


def summarize_replay_action_agreement(
    source_actions: Sequence[Mapping[str, Any]],
    replay_results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    selected_source_actions = select_agent_executions(source_actions)
    denominator = max(len(selected_source_actions), len(replay_results))
    matching_action_count = 0
    action_mismatches = []
    for index in range(denominator):
        source = selected_source_actions[index] if index < len(selected_source_actions) else None
        replay = replay_results[index] if index < len(replay_results) else None
        reasons = []
        if source is None:
            reasons.append("added")
        if replay is None:
            reasons.append("missing")
        if source is not None and replay is not None:
            source_action = source.get("action") or {}
            replay_request = replay.get("request") or {}
            source_name = str(source_action.get("name") or "")
            replay_name = str(replay_request.get("name") or "")
            if source_name != replay_name:
                reasons.append("name")
            source_params = _normalize_run_scoped_action_params(
                source_name,
                source_action.get("params") or {},
            )
            replay_params = _normalize_run_scoped_action_params(
                replay_name,
                replay_request.get("params") or {},
            )
            if source_params != replay_params:
                reasons.append("parameters")
            source_ok = (source.get("result") or {}).get("ok")
            replay_ok = (replay.get("result") or {}).get("ok")
            if (
                not isinstance(source_ok, bool)
                or not isinstance(replay_ok, bool)
                or source_ok != replay_ok
            ):
                reasons.append("outcome")
        if reasons:
            action_mismatches.append(
                {
                    "ordinal": index + 1,
                    "reasons": reasons,
                }
            )
        else:
            matching_action_count += 1
    return {
        "source_action_count": len(selected_source_actions),
        "replay_action_count": len(replay_results),
        "matching_action_count": matching_action_count,
        "replay_action_agreement": (
            matching_action_count / denominator if denominator else 0.0
        ),
        "action_mismatches": action_mismatches,
    }


def summarize_paired_action_replay(
    *,
    source_actions: Sequence[Mapping[str, Any]],
    replay_results: Sequence[Mapping[str, Any]],
    source_requests: Sequence[Mapping[str, Any]],
    replay_requests: Sequence[Mapping[str, Any]],
    interactive_resource_ids: set[str],
) -> Dict[str, Any]:
    agreement = summarize_replay_action_agreement(source_actions, replay_results)
    source_generated = {
        str(item.get("resource_id") or "")
        for item in source_requests
        if str(item.get("resource_id") or "") in interactive_resource_ids
    }
    replay_generated = {
        str(item.get("resource_id") or "")
        for item in replay_requests
        if str(item.get("resource_id") or "") in interactive_resource_ids
    }
    reproduced = source_generated.intersection(replay_generated)
    return {
        **agreement,
        "source_generated_family_count": len(source_generated),
        "replay_generated_family_count": len(replay_generated),
        "reproduced_family_count": len(reproduced),
        "replay_family_reproduction": (
            len(reproduced) / len(source_generated) if source_generated else 0.0
        ),
        "source_generated_family_ids": sorted(source_generated),
        "replay_generated_family_ids": sorted(replay_generated),
        "reproduced_family_ids": sorted(reproduced),
        "missing_source_family_ids": sorted(source_generated - replay_generated),
    }


def aggregate_paired_action_replays(
    manifests: Sequence[Mapping[str, Any]],
    *,
    pipeline_seconds: Sequence[float] | None = None,
) -> Dict[str, Any]:
    def distribution(values: Sequence[float]) -> Dict[str, float]:
        values = [float(value) for value in values]
        if not values:
            return {
                "mean": 0.0,
                "sample_standard_deviation": 0.0,
                "median": 0.0,
                "minimum": 0.0,
                "maximum": 0.0,
            }
        return {
            "mean": fmean(values),
            "sample_standard_deviation": stdev(values) if len(values) > 1 else 0.0,
            "median": median(values),
            "minimum": min(values),
            "maximum": max(values),
        }

    if pipeline_seconds is not None and len(pipeline_seconds) != len(manifests):
        raise ValueError("pipeline_seconds must contain one value per replay manifest")
    result = {
        "schema_version": "1.0.0",
        "pair_count": len(manifests),
        "replay_action_agreement": distribution(
            [float(item.get("replay_action_agreement") or 0.0) for item in manifests]
        ),
        "replay_family_reproduction": distribution(
            [float(item.get("replay_family_reproduction") or 0.0) for item in manifests]
        ),
        "pairs": [dict(item) for item in manifests],
    }
    if pipeline_seconds is not None:
        result["end_to_end_pipeline_time_seconds"] = distribution(pipeline_seconds)
    return result


def recovery_replay(
    source_run_dir: Path,
    output_dir: Path,
    *,
    tool_exe: Path,
    resource_manifest_path: Path,
    behavior_manifest_path: Path,
    size_semantics: Mapping[str, str] | None = None,
    content_semantics: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(source_run_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Replay output already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    source_manifest = _read_json(source_run_dir / "run_manifest.json")
    snapshot_manifest = _read_json(source_run_dir / "snapshot_manifest.json")
    cache_dirs = [
        path
        for path in (source_run_dir / "snapshot").rglob("chromium.cache.cache_data")
        if path.is_dir()
    ]
    if not cache_dirs:
        raise RuntimeError("Source run does not contain chromium.cache.cache_data")

    csv_path = output_dir / "chromecacheview.csv"
    extracted_dir = output_dir / "extracted_cache"
    base_origin = str(source_manifest.get("web_origin") or "").strip()
    if not base_origin:
        raise ValueError("Source run manifest is missing web_origin")
    tool_run = run_chromecacheview(
        Path(tool_exe),
        cache_dirs[0],
        csv_path,
        extracted_dir,
        url_prefix=base_origin,
    )
    records = parse_cache_csv(csv_path)
    requests = _read_jsonl(source_run_dir / "archive_requests.jsonl")
    comparison = compare_autonomous_run(
        _read_json(resource_manifest_path),
        _read_json(behavior_manifest_path),
        requests,
        snapshot_manifest,
        records,
        extracted_dir,
        base_origin=base_origin,
        size_semantics=size_semantics,
        content_semantics=content_semantics,
    )
    _write_json(output_dir / "tool_run.json", tool_run.__dict__)
    _write_json(output_dir / "comparison.json", comparison)
    replay_manifest = {
        "schema_version": "1.0.0",
        "replay_type": "recovery",
        "source_run_id": str(source_manifest.get("run_id") or ""),
        "source_run_dir": str(source_run_dir.resolve()),
        "comparison": comparison.get("summary") or {},
        "excluded_from_campaign_metrics": True,
    }
    _write_json(output_dir / "replay_manifest.json", replay_manifest)
    return replay_manifest
