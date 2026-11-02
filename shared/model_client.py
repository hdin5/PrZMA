from __future__ import annotations

import os
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import requests


_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_PRICE_TABLE_VERSION = "openai-api-standard-2026-07-18"
_PRICE_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
_STANDARD_PRICES_PER_MILLION = {
    "gpt-5.6-terra": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    }
}


@dataclass(frozen=True)
class ModelCompletion:
    content: str
    usage: Dict[str, int]
    metadata: Dict[str, Any]

    def to_record(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            **self.metadata,
            "usage": dict(self.usage),
        }


class JsonlUsageLedger:
    """Thread-safe append-only sink for non-secret model-call metadata."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def __call__(self, record: Mapping[str, Any]) -> None:
        serialized = json.dumps(dict(record), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")


def _is_reasoning_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith(_REASONING_MODEL_PREFIXES)


def _estimate_standard_cost(
    model_requested: str,
    model_returned: str,
    usage: Mapping[str, int],
) -> Dict[str, Any]:
    candidates = [str(model_returned or "").lower(), str(model_requested or "").lower()]
    selected = None
    for model_name, rates in _STANDARD_PRICES_PER_MILLION.items():
        if any(value == model_name or value.startswith(model_name + "-") for value in candidates):
            selected = rates
            break
    if selected is None:
        return {
            "price_table_version": None,
            "price_source_url": None,
            "estimated_cost_usd": None,
        }

    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    cached_tokens = min(
        input_tokens,
        max(0, int(usage.get("cached_input_tokens") or 0)),
    )
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    estimated = (
        uncached_tokens * selected["input"]
        + cached_tokens * selected["cached_input"]
        + output_tokens * selected["output"]
    ) / 1_000_000.0
    return {
        "price_table_version": _PRICE_TABLE_VERSION,
        "price_source_url": _PRICE_SOURCE_URL,
        "estimated_cost_usd": round(estimated, 12),
    }


def summarize_usage_records(records: List[Mapping[str, Any]]) -> Dict[str, Any]:
    token_fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    summary: Dict[str, Any] = {
        "call_count": len(records),
        "successful_call_count": 0,
        "failed_call_count": 0,
        **{field: 0 for field in token_fields},
        "latency_ms": 0.0,
        "estimated_cost_usd": 0.0,
        "unpriced_successful_call_count": 0,
        "calls_by_stage": {},
        "price_table_versions": [],
    }
    versions = set()
    stages: Dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "")
        if status == "success":
            summary["successful_call_count"] += 1
        else:
            summary["failed_call_count"] += 1
        stage = str(record.get("stage") or "unspecified")
        stages[stage] = stages.get(stage, 0) + 1
        usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
        for field in token_fields:
            summary[field] += max(0, int(usage.get(field) or 0))
        summary["latency_ms"] += max(0.0, float(record.get("latency_ms") or 0.0))
        cost = record.get("estimated_cost_usd")
        if cost is None:
            if status == "success":
                summary["unpriced_successful_call_count"] += 1
        else:
            summary["estimated_cost_usd"] += max(0.0, float(cost))
        version = str(record.get("price_table_version") or "").strip()
        if version:
            versions.add(version)
    summary["latency_ms"] = round(summary["latency_ms"], 3)
    summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"], 12)
    summary["calls_by_stage"] = dict(sorted(stages.items()))
    summary["price_table_versions"] = sorted(versions)
    return summary


def build_chat_body(
    model: str,
    messages: List[Dict[str, str]],
    reasoning_effort: Optional[str] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if _is_reasoning_model(model):
        effort = (reasoning_effort or "medium").strip().lower()
        if effort not in _REASONING_EFFORTS:
            raise ValueError(f"Unsupported reasoning effort: {effort}")
        body["reasoning_effort"] = effort
    elif temperature is not None:
        body["temperature"] = float(temperature)
    return body


def request_json_completion_with_metadata(
    model: str,
    api_key: str,
    messages: List[Dict[str, str]],
    reasoning_effort: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout_sec: int = 90,
    *,
    stage: str = "unspecified",
    retry_index: int = 0,
    usage_sink: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> ModelCompletion:
    effort = reasoning_effort or os.getenv("OPENAI_REASONING_EFFORT", "medium")
    body = build_chat_body(
        model=model,
        messages=messages,
        reasoning_effort=effort,
        temperature=temperature,
    )
    started = time.perf_counter()
    response = None
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Provider error {response.status_code}: {response.text[:800]}")
        response.raise_for_status()
        data = response.json()
        raw_usage = data.get("usage") or {}
        prompt_details = raw_usage.get("prompt_tokens_details") or {}
        completion_details = raw_usage.get("completion_tokens_details") or {}
        usage = {
            "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
            "output_tokens": int(raw_usage.get("completion_tokens") or 0),
            "total_tokens": int(raw_usage.get("total_tokens") or 0),
            "cached_input_tokens": int(prompt_details.get("cached_tokens") or 0),
            "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
        }
        returned_model = str(data.get("model") or model)
        metadata = {
            "stage": str(stage),
            "model_requested": model,
            "model_returned": returned_model,
            "request_id": str(data.get("id") or ""),
            "reasoning_effort": body.get("reasoning_effort"),
            "temperature": body.get("temperature"),
            "retry_index": max(0, int(retry_index)),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "status": "success",
            "error_type": None,
            "http_status": int(response.status_code),
            **_estimate_standard_cost(model, returned_model, usage),
        }
        completion = ModelCompletion(
            content=str(data["choices"][0]["message"]["content"]),
            usage=usage,
            metadata=metadata,
        )
        if usage_sink is not None:
            usage_sink(completion.to_record())
        return completion
    except Exception as exc:
        provider_error_code = None
        if response is not None:
            try:
                error_payload = response.json().get("error") or {}
                if isinstance(error_payload, Mapping):
                    provider_error_code = str(error_payload.get("code") or "") or None
            except Exception:
                provider_error_code = None
        failure = ModelCompletion(
            content="",
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            },
            metadata={
                "stage": str(stage),
                "model_requested": model,
                "model_returned": "",
                "request_id": "",
                "reasoning_effort": body.get("reasoning_effort"),
                "temperature": body.get("temperature"),
                "retry_index": max(0, int(retry_index)),
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "status": "error",
                "error_type": type(exc).__name__,
                "http_status": int(response.status_code) if response is not None else None,
                "provider_error_code": provider_error_code,
                "price_table_version": None,
                "price_source_url": None,
                "estimated_cost_usd": None,
            },
        )
        if usage_sink is not None:
            usage_sink(failure.to_record())
        raise


def request_json_completion(
    model: str,
    api_key: str,
    messages: List[Dict[str, str]],
    reasoning_effort: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout_sec: int = 90,
) -> str:
    return request_json_completion_with_metadata(
        model=model,
        api_key=api_key,
        messages=messages,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        timeout_sec=timeout_sec,
    ).content
