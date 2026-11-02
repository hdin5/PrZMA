from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit


FILE_SUFFIXES = {
    "csv",
    "json",
    "pdf",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "zip",
    "txt",
}
COMMIT_TERMS = {"apply", "submit", "search"}
LOAD_TERMS = {"load", "preview", "request", "generate"}
OPEN_TERMS = {"open", "show", "view"}
GENERIC_EFFECT_TERMS = {
    "a",
    "an",
    "the",
    "apply",
    "submit",
    "search",
    "load",
    "request",
    "generate",
    "open",
    "show",
    "view",
}


def effect_key(family: str, representation: str = "") -> str:
    normalized = representation.strip().lower()
    return f"{family}:{normalized}" if normalized else family


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _visible_representation(text: str) -> str:
    return next(
        (word for word in _words(text) if word not in GENERIC_EFFECT_TERMS),
        "",
    )


def _route_family(href: str) -> str:
    parts = [part.lower() for part in urlsplit(href).path.split("/") if part]
    if not parts:
        return "root"
    if len(parts) == 1:
        return f"{parts[0]}:index"
    return parts[0]


def _same_document(left: str, right: str) -> bool:
    first = urlsplit(left)
    second = urlsplit(right)
    return (
        first.scheme.lower(),
        first.netloc.lower(),
        first.path.rstrip("/") or "/",
        first.query,
    ) == (
        second.scheme.lower(),
        second.netloc.lower(),
        second.path.rstrip("/") or "/",
        second.query,
    )


def candidate_from_control(
    control: Mapping[str, Any],
    *,
    page_url: str,
) -> Optional[Dict[str, Any]]:
    tag = str(control.get("tag") or "").lower()
    control_type = str(control.get("type") or "").lower()
    href = str(control.get("href") or "").strip()
    selector = str(control.get("selector") or "").strip()
    text = " ".join(
        str(control.get("text") or control.get("label") or "").lower().split()
    )
    words = set(_words(text))
    role = str(control.get("role") or "").lower()
    suffix = (
        PurePosixPath(urlsplit(href).path).suffix.lower().lstrip(".")
        if href
        else ""
    )

    if tag == "a" and suffix in FILE_SUFFIXES:
        key = effect_key("file_acquisition", "jpeg" if suffix == "jpg" else suffix)
    elif tag == "a" and href:
        key = effect_key("document_navigation", _route_family(href))
    elif not (
        tag == "button"
        or role == "button"
        or (tag == "input" and control_type in {"button", "submit"})
    ):
        return None
    elif control_type == "submit" or words.intersection(COMMIT_TERMS):
        representation = (
            "search" if "search" in words else _visible_representation(text)
        )
        key = effect_key("form_commit", representation)
    elif words.intersection(LOAD_TERMS):
        key = effect_key("dynamic_request", _visible_representation(text))
    elif words.intersection(OPEN_TERMS):
        key = effect_key("content_open", _visible_representation(text))
    else:
        return None

    return {
        "effect_key": key,
        "page_url": str(page_url),
        "selector": selector,
        "href": href,
        "text": text[:120],
    }


def proposed_effect_key(
    name: str,
    params: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> str:
    ledger = observations.get("semantic_effect_ledger")
    if not isinstance(ledger, Mapping):
        return ""
    selector = str(params.get("selector") or "").strip()
    href = str(params.get("url") or params.get("href") or "").strip()
    candidates = ledger.get("observed_effect_candidates") or []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if selector and str(candidate.get("selector") or "") == selector:
            return str(candidate.get("effect_key") or "")
        if href and str(candidate.get("href") or "") == href:
            return str(candidate.get("effect_key") or "")
    return ""


def preferred_parameter_commit_effects(
    candidates: Iterable[Mapping[str, Any]],
) -> set[str]:
    compatible_effects = {
        str(candidate.get("effect_key") or "")
        for candidate in candidates
        if str(candidate.get("effect_key") or "").startswith(
            ("form_commit", "dynamic_request")
        )
    }
    non_search_effects = {
        key for key in compatible_effects if key != "form_commit:search"
    }
    return non_search_effects or compatible_effects


def normalized_action_signature(
    name: str,
    params: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> str:
    effect = proposed_effect_key(name, params, observations)
    if effect:
        return f"{name}|{effect}"
    return f"{name}|{json.dumps(dict(params), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def build_semantic_effect_ledger(
    entries: Sequence[Mapping[str, Any]],
    agent_id: str,
) -> Dict[str, Any]:
    current_url = ""
    controls_by_page: Dict[str, Dict[str, Dict[str, Any]]] = {}
    discovered: Dict[str, Dict[str, Any]] = {}
    observed_by_source: Dict[str, Dict[str, Any]] = {}
    covered: set[str] = set()
    unresolved: Dict[str, Dict[str, Any]] = {}
    pending_parameter_commit: Optional[Dict[str, Any]] = None

    for entry in entries:
        if str(entry.get("agent_id") or "") != agent_id:
            continue
        if str(entry.get("kind") or "") != "action":
            continue
        action = entry.get("action") if isinstance(entry.get("action"), Mapping) else {}
        result = entry.get("result") if isinstance(entry.get("result"), Mapping) else {}
        outputs = result.get("outputs") if isinstance(result.get("outputs"), Mapping) else {}
        params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
        name = str(action.get("name") or "")
        result_url = str(outputs.get("current_url") or "").strip()

        if name == "browser.goto":
            requested_url = str(params.get("url") or "").strip()
            target_url = result_url or requested_url
            candidate = next(
                (
                    item
                    for item in observed_by_source.values()
                    if str(item.get("href") or "") in {requested_url, target_url}
                ),
                None,
            )
            if candidate is not None:
                candidate_key = str(candidate["effect_key"])
                if result.get("ok") is True:
                    covered.add(candidate_key)
                    unresolved.pop(candidate_key, None)
                elif candidate_key not in covered:
                    unresolved[candidate_key] = {
                        "effect_key": candidate_key,
                        "page_url": current_url,
                        "href": requested_url,
                        "error": str(result.get("error") or "action failed")[:240],
                    }
            if result.get("ok") is True:
                current_url = target_url
            continue

        if name == "browser.get_clickables" and result.get("ok") is True:
            if result_url:
                current_url = result_url
            clickables = outputs.get("clickables")
            samples = (
                clickables.get("sample") or []
                if isinstance(clickables, Mapping)
                else []
            )
            page_controls = controls_by_page.setdefault(current_url, {})
            for control in samples:
                if not isinstance(control, Mapping):
                    continue
                candidate = candidate_from_control(control, page_url=current_url)
                if candidate is None:
                    continue
                selector = str(candidate.get("selector") or "")
                if selector:
                    page_controls[selector] = candidate
                source_key = "\0".join(
                    [
                        current_url,
                        selector,
                        str(candidate.get("href") or ""),
                        str(candidate["effect_key"]),
                    ]
                )
                observed_by_source[source_key] = candidate
                candidate_key = str(candidate["effect_key"])
                discovered.setdefault(candidate_key, candidate)
                if candidate_key.startswith("document_navigation:") and _same_document(
                    str(candidate.get("href") or ""), current_url
                ):
                    covered.add(candidate_key)
            continue

        action_page = current_url
        if name == "browser.select_option" and result.get("ok") is True:
            parameter_selector = str(params.get("selector") or "parameter control")
            pending_parameter_commit = {
                "effect_key": "parameter_commit",
                "page_url": action_page,
                "selector": "",
                "href": "",
                "text": f"commit the selected parameter change from {parameter_selector}"[:120],
            }
        if name in {"browser.click", "browser.smart_click"}:
            selector = str(params.get("selector") or "")
            candidate = controls_by_page.get(action_page, {}).get(selector)
            if candidate is None and name == "browser.smart_click":
                picked = outputs.get("picked")
                if not isinstance(picked, Mapping) and outputs.get("href"):
                    picked = {
                        "tag": "a",
                        "href": outputs.get("href"),
                        "text": outputs.get("text"),
                    }
                if isinstance(picked, Mapping):
                    candidate = candidate_from_control(picked, page_url=action_page)
            if candidate is not None:
                candidate_key = str(candidate["effect_key"])
                if result.get("ok") is True:
                    covered.add(candidate_key)
                    unresolved.pop(candidate_key, None)
                    if candidate_key.startswith(("form_commit", "dynamic_request")):
                        preferred_effects = preferred_parameter_commit_effects(
                            controls_by_page.get(action_page, {}).values()
                        )
                        if (
                            pending_parameter_commit is None
                            or not preferred_effects
                            or candidate_key in preferred_effects
                        ):
                            pending_parameter_commit = None
                elif candidate_key not in covered:
                    unresolved[candidate_key] = {
                        "effect_key": candidate_key,
                        "page_url": action_page,
                        "selector": selector,
                        "error": str(result.get("error") or "action failed")[:240],
                    }

        if result_url:
            current_url = result_url

    uncovered = [
        discovered[key]
        for key in sorted(discovered)
        if key not in covered
    ]
    if pending_parameter_commit is not None:
        uncovered.append(pending_parameter_commit)

    exposed_candidates: Dict[str, Dict[str, Any]] = {
        source: candidate
        for source, candidate in observed_by_source.items()
        if str(candidate.get("page_url") or "") == current_url
    }
    for candidate in uncovered:
        source = "\0".join(
            [
                str(candidate.get("page_url") or ""),
                str(candidate.get("selector") or ""),
                str(candidate.get("href") or ""),
                str(candidate.get("effect_key") or ""),
            ]
        )
        exposed_candidates.setdefault(source, candidate)

    return {
        "covered_effect_keys": sorted(covered),
        "observed_effect_candidates": list(exposed_candidates.values()),
        "uncovered_effect_candidates": uncovered,
        "unresolved_effect_failures": list(unresolved.values()),
    }
