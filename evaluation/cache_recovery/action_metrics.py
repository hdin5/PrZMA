from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping

from Automation_Agent.semantic_effects import build_semantic_effect_ledger


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _proposal_signature(entry: Mapping[str, Any]) -> str:
    action = entry.get("action") if isinstance(entry.get("action"), Mapping) else {}
    return json.dumps(
        {
            "name": str(action.get("name") or ""),
            "params": dict(action.get("params") or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def calculate_posthoc_artifact_efficiency(
    actions: Iterable[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    action_rows = list(actions)
    proposals = [
        entry
        for entry in action_rows
        if str(entry.get("kind") or "") == "proposal"
        and str(entry.get("origin") or "") == "agent"
        and isinstance(entry.get("action"), Mapping)
        and (entry.get("action") or {}).get("name")
    ]
    executions = [
        entry
        for entry in action_rows
        if str(entry.get("kind") or "") == "action"
        and str(entry.get("origin") or "") == "agent"
        and isinstance(entry.get("action"), Mapping)
        and (entry.get("action") or {}).get("name")
        and not (
            isinstance(entry.get("result"), Mapping)
            and (entry.get("result") or {}).get("skipped") is True
        )
    ]

    first_seen: Dict[tuple[str, str], datetime] = {}
    for request in requests:
        if str(request.get("classification") or "") != "interactive":
            continue
        observed_at = _timestamp(request.get("ts"))
        if observed_at is None:
            continue
        target = (
            str(request.get("resource_id") or request.get("request_url") or ""),
            str(request.get("variant_id") or "default"),
        )
        previous = first_seen.get(target)
        if previous is None or observed_at < previous:
            first_seen[target] = observed_at

    last_new_target_at = max(first_seen.values(), default=None)
    actions_after_last_new_target = (
        sum(
            1
            for entry in proposals
            if (proposal_time := _timestamp(entry.get("ts"))) is not None
            and proposal_time > last_new_target_at
        )
        if last_new_target_at is not None
        else len(proposals)
    )

    duplicate_navigation_count = 0
    previous_navigation_url = ""
    for entry in proposals:
        action = entry.get("action") or {}
        if str(action.get("name") or "") != "browser.goto":
            previous_navigation_url = ""
            continue
        params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
        url = str(params.get("url") or "").strip()
        if url and url == previous_navigation_url:
            duplicate_navigation_count += 1
        previous_navigation_url = url

    def longest_signature_streak(entries: Iterable[Mapping[str, Any]]) -> int:
        longest_streak = 0
        current_streak = 0
        previous_signature = ""
        for entry in entries:
            signature = _proposal_signature(entry)
            if signature == previous_signature:
                current_streak += 1
            else:
                previous_signature = signature
                current_streak = 1
            longest_streak = max(longest_streak, current_streak)
        return longest_streak

    proposal_streak = longest_signature_streak(proposals)
    executed_streak = longest_signature_streak(executions)

    return {
        "first_seen_interactive_target_count": len(first_seen),
        "last_new_interactive_target_at": (
            last_new_target_at.isoformat() if last_new_target_at is not None else ""
        ),
        "actions_after_last_new_target": actions_after_last_new_target,
        "duplicate_navigation_count": duplicate_navigation_count,
        "longest_identical_signature_streak": proposal_streak,
        "longest_identical_proposal_signature_streak": proposal_streak,
        "longest_identical_executed_signature_streak": executed_streak,
    }


def calculate_action_metrics(
    actions: Iterable[Mapping[str, Any]],
    tool_plan: Mapping[str, Any],
    *,
    evaluation_mode: str = "controlled",
) -> Dict[str, Any]:
    action_entries = list(actions)
    required = {str(value) for value in tool_plan.get("required_actions") or []}
    support = {str(value) for value in tool_plan.get("support_actions") or []}
    triggers = {str(value) for value in tool_plan.get("trigger_actions") or []}
    forbidden = {str(value) for value in tool_plan.get("forbidden_actions") or []}
    allowed = required | support | triggers

    events = []
    proposed = []
    origins: Counter[str] = Counter()
    proposal_origins: Counter[str] = Counter()
    by_type: Dict[str, Counter[str]] = defaultdict(Counter)
    internal_event_count = 0
    explicit_proposals = any(str(entry.get("kind") or "") == "proposal" for entry in action_entries)
    validation_entries = []
    autonomous_executions = []
    system_executions = []
    for entry in action_entries:
        action = entry.get("action") or {}
        name = action.get("name") if isinstance(action, Mapping) else None
        result = entry.get("result") or {}
        ok = isinstance(result, Mapping) and result.get("ok") is True
        kind = str(entry.get("kind") or "")
        origin = str(entry.get("origin") or entry.get("kind") or "unknown")

        if kind in {"validation", "validation_repair"}:
            validation_entries.append((str(name) if name else None, result, origin))
            if explicit_proposals:
                continue

        if not name:
            continue
        name = str(name)

        if kind == "full_trigger":
            internal_event_count += 1
            continue

        if kind == "proposal":
            rejected = isinstance(result, Mapping) and result.get("skipped") is True
            proposed.append((name, rejected, origin, result))
            proposal_origins[origin] += 1
            by_type[name]["proposed"] += 1
            by_type[name]["rejected"] += int(rejected)
            continue

        rejected = isinstance(result, Mapping) and result.get("skipped") is True
        if not explicit_proposals:
            proposed.append((name, rejected, origin, result))
            proposal_origins[origin] += 1
            by_type[name]["proposed"] += 1
            by_type[name]["rejected"] += int(rejected)

        is_execution = not rejected and (
            kind in {"bootstrap", "action", "finalize", "cleanup"}
            or (not kind and isinstance(result, Mapping) and "ok" in result)
        )
        if is_execution:
            events.append((name, ok, origin))
            origins[origin] += 1
            by_type[name]["events"] += 1
            by_type[name]["successful"] += int(ok)
            if origin == "agent" and kind == "action":
                autonomous_executions.append((name, ok, entry))
            if origin in {"system_bootstrap", "system_cleanup"}:
                system_executions.append((name, ok, entry))

    successful_names = {name for name, ok, _origin in events if ok}
    unexpected = [
        name
        for name, _rejected, _origin, result in proposed
        if name not in allowed or (isinstance(result, Mapping) and result.get("code") == "UNEXPECTED_ACTION")
    ]
    forbidden_events = [
        name
        for name, _rejected, _origin, result in proposed
        if name in forbidden or (isinstance(result, Mapping) and result.get("code") == "FORBIDDEN_ACTION")
    ]
    rejected_count = sum(1 for _name, rejected, _origin, _result in proposed if rejected)
    indices: Dict[str, int] = {}
    for index, (name, ok, _origin) in enumerate(events):
        if ok and name not in indices:
            indices[name] = index
    ordered = [name for name in ("browser.launch", "browser.goto", "browser.close") if name in required]
    sequence_conformant = all(name in indices for name in ordered) and all(
        indices[left] < indices[right] for left, right in zip(ordered, ordered[1:])
    )

    result = {
        "schema_version": "1.0.0",
        "required_actions": sorted(required),
        "successful_required_actions": sorted(required.intersection(successful_names)),
        "required_action_coverage": (
            len(required.intersection(successful_names)) / len(required) if required else 1.0
        ),
        "action_event_count": len(events),
        "action_success_rate": (sum(1 for _name, ok, _origin in events if ok) / len(events)) if events else 0.0,
        "vm_execution_count": len(events),
        "vm_execution_success_rate": (
            sum(1 for _name, ok, _origin in events if ok) / len(events)
        ) if events else 0.0,
        "proposed_action_count": len(proposed),
        "rejected_action_count": rejected_count,
        "rejected_action_rate": (rejected_count / len(proposed)) if proposed else 0.0,
        "internal_event_count": internal_event_count,
        "unexpected_action_count": len(unexpected),
        "unexpected_action_rate": (len(unexpected) / len(proposed)) if proposed else 0.0,
        "forbidden_action_violations": len(forbidden_events),
        "sequence_conformant": sequence_conformant,
        "origins": dict(sorted(origins.items())),
        "proposal_origins": dict(sorted(proposal_origins.items())),
        "by_type": {name: dict(counts) for name, counts in sorted(by_type.items())},
    }
    if evaluation_mode == "autonomous_cache_recovery":
        validation_interventions = [
            item for item in validation_entries if isinstance(item[1], Mapping)
        ]
        parameter_variants = {
            json.dumps(
                (entry.get("action") or {}).get("params") or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for entry in action_entries
            if str(entry.get("kind") or "") == "proposal"
            and isinstance(entry.get("action"), Mapping)
            and (entry.get("action") or {}).get("name")
        }
        result.update(
            {
                "autonomous_proposed_action_count": len(proposed),
                "autonomous_executed_action_count": len(autonomous_executions),
                "autonomous_action_success_rate": (
                    sum(1 for _name, ok, _entry in autonomous_executions if ok)
                    / len(autonomous_executions)
                    if autonomous_executions
                    else 0.0
                ),
                "system_execution_count": len(system_executions),
                "validation_intervention_count": len(validation_interventions),
                "validation_intervention_rate": (
                    len(validation_interventions) / len(proposed) if proposed else 0.0
                ),
                "distinct_autonomous_action_types": sorted(
                    {name for name, _ok, _entry in autonomous_executions}
                ),
                "distinct_proposed_parameter_variants": len(parameter_variants),
            }
        )
        provenance: Dict[str, Dict[str, Any]] = {
            key: {"proposed": 0, "executed": 0, "action_types": set()}
            for key in (
                "tmi_required",
                "framework_support",
                "system_bootstrap",
                "system_cleanup",
            )
        }

        def provenance_key(entry: Mapping[str, Any], name: str) -> str | None:
            origin = str(entry.get("origin") or "")
            if origin == "system_bootstrap":
                return "system_bootstrap"
            if origin == "system_cleanup":
                return "system_cleanup"
            if name in support:
                return "framework_support"
            if name in required:
                return "tmi_required"
            return None

        for entry in action_entries:
            action = entry.get("action") if isinstance(entry.get("action"), Mapping) else {}
            name = str(action.get("name") or "")
            if not name:
                continue
            kind = str(entry.get("kind") or "")
            result_row = entry.get("result") if isinstance(entry.get("result"), Mapping) else {}
            bucket = provenance_key(entry, name)
            if bucket is None:
                continue
            if kind == "proposal":
                provenance[bucket]["proposed"] += 1
                provenance[bucket]["action_types"].add(name)
            elif result_row.get("skipped") is not True and kind in {
                "bootstrap",
                "action",
                "finalize",
                "cleanup",
            }:
                provenance[bucket]["executed"] += 1
                provenance[bucket]["action_types"].add(name)

        result["capability_provenance"] = {
            key: {
                "proposed": value["proposed"],
                "executed": value["executed"],
                "action_types": sorted(value["action_types"]),
            }
            for key, value in provenance.items()
        }

        agent_ids = sorted(
            {
                str(entry.get("agent_id") or "")
                for entry in action_entries
                if str(entry.get("agent_id") or "")
            }
        )
        covered_effects: set[str] = set()
        for agent_id in agent_ids:
            ledger = build_semantic_effect_ledger(action_entries, agent_id)
            covered_effects.update(ledger.get("covered_effect_keys") or [])
        result["covered_semantic_effect_keys"] = sorted(covered_effects)
        result["covered_semantic_effect_count"] = len(covered_effects)
        result["executions_per_effect"] = (
            len(autonomous_executions) / len(covered_effects)
            if covered_effects
            else 0.0
        )
        result["equivalent_repeat_count"] = sum(
            1
            for _name, validation_result, _origin in validation_entries
            if isinstance(validation_result, Mapping)
            and validation_result.get("code") == "REDUNDANT_ACTION_EFFECT"
        )
        result["rejected_action_count"] = len(validation_interventions)
        result["rejected_action_rate"] = (
            len(validation_interventions) / len(proposed) if proposed else 0.0
        )
    return result
