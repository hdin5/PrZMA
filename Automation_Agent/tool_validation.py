from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from Automation_Agent.semantic_effects import (
    preferred_parameter_commit_effects,
    proposed_effect_key,
)


def _names(values: Iterable[object]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port = parsed.port or default_port
    return f"{parsed.scheme.lower()}://{host}:{port}"


@dataclass(frozen=True)
class ValidationPolicy:
    required_actions: Tuple[str, ...]
    forbidden_actions: Tuple[str, ...]
    support_actions: Tuple[str, ...]
    trigger_actions: Tuple[str, ...]
    allowed_origins: Tuple[str, ...]
    max_repairs: int = 5
    mode: str = "autonomous"

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> Optional["ValidationPolicy"]:
        contract = config.get("tool_validation")
        evaluation = config.get("evaluation")
        if (
            not isinstance(contract, dict)
            or not isinstance(evaluation, dict)
            or evaluation.get("mode") != "autonomous_cache_recovery"
        ):
            return None
        resources = config.get("available_resources") or {}
        web_url = (
            str(resources.get("web_url") or "").strip()
            if isinstance(resources, dict)
            else ""
        )
        configured_origins = evaluation.get("allowed_origins") or []
        if not configured_origins and web_url:
            configured_origins = [_origin(web_url)]
        allowed_origins = tuple(
            _origin(value)
            for value in _names(configured_origins)
            if _origin(value)
        )
        return cls(
            required_actions=_names(contract.get("required_actions") or []),
            forbidden_actions=_names(contract.get("forbidden_actions") or []),
            support_actions=_names(contract.get("support_actions") or []),
            trigger_actions=_names(contract.get("trigger_actions") or []),
            allowed_origins=allowed_origins,
            max_repairs=max(1, int(evaluation.get("max_validation_repairs") or 5)),
        )

    @property
    def allowed_actions(self) -> set[str]:
        return set(self.required_actions) | set(self.support_actions) | set(self.trigger_actions)

    @property
    def agent_required_actions(self) -> Tuple[str, ...]:
        system_actions = set(self.support_actions) | set(self.trigger_actions)
        return tuple(name for name in self.required_actions if name not in system_actions)


@dataclass
class ValidationState:
    successful_actions: set[str] = field(default_factory=set)
    trigger_completed: bool = False
    repair_count: int = 0

    def observe(self, name: Optional[str], result: Dict[str, Any], policy: ValidationPolicy) -> None:
        if name and isinstance(result, dict) and result.get("ok") is True:
            self.successful_actions.add(name)
            self.repair_count = 0
            if name in policy.trigger_actions:
                self.trigger_completed = True


@dataclass(frozen=True)
class ValidationDecision:
    allowed: bool
    code: str
    params: Dict[str, Any]
    feedback: str


def evaluate_candidate(
    name: Optional[str],
    params: Dict[str, Any],
    state: ValidationState,
    policy: ValidationPolicy,
    runtime_observations: Optional[Dict[str, Any]] = None,
) -> ValidationDecision:
    candidate_params = dict(params or {})

    if name is not None and name in policy.forbidden_actions:
        return ValidationDecision(False, "FORBIDDEN_ACTION", candidate_params, f"{name} is forbidden by the TMI plan.")

    if name is not None and name not in policy.allowed_actions:
        return ValidationDecision(False, "UNEXPECTED_ACTION", candidate_params, f"{name} is outside the TMI-bounded action space.")

    if policy.mode == "autonomous":
        observations = runtime_observations or {}
        if name is not None:
            recent_actions = observations.get("recent_actions") or []
            last_action = (
                recent_actions[-1]
                if isinstance(recent_actions, list)
                and recent_actions
                and isinstance(recent_actions[-1], dict)
                else {}
            )
            if (
                name != "browser.goto"
                and name not in policy.trigger_actions
                and last_action.get("name") == name
                and last_action.get("ok") is True
                and isinstance(last_action.get("params"), dict)
                and last_action.get("params") == candidate_params
            ):
                return ValidationDecision(
                    False,
                    "REDUNDANT_ACTION_SIGNATURE",
                    candidate_params,
                    "The same action and parameters already succeeded in the unchanged current state; choose a materially different effect or state change.",
                )
            if name in {
                "browser.click",
                "browser.smart_click",
                "browser.select_option",
                "browser.smart_type",
                "browser.type",
                "browser.press",
                "browser.scroll",
            } and _current_page_observation_is_stale(observations):
                return ValidationDecision(
                    False,
                    "STALE_PAGE_OBSERVATION",
                    candidate_params,
                    "The current page changed or was scrolled after the latest DOM observation; re-observe the current page before choosing a target-dependent action.",
                )
            effect = proposed_effect_key(name, candidate_params, observations)
            ledger = observations.get("semantic_effect_ledger") or {}
            covered_effects = set(
                ledger.get("covered_effect_keys") or []
                if isinstance(ledger, dict)
                else []
            )
            uncovered_effects = {
                str(item.get("effect_key") or "")
                for item in (ledger.get("uncovered_effect_candidates") or [])
                if isinstance(item, dict)
            } if isinstance(ledger, dict) else set()
            observed_effects = (
                ledger.get("observed_effect_candidates") or []
                if isinstance(ledger, dict)
                else []
            )
            preferred_commit_effects = preferred_parameter_commit_effects(
                item for item in observed_effects if isinstance(item, dict)
            )
            parameter_variant_commit = (
                "parameter_commit" in uncovered_effects
                and effect in preferred_commit_effects
            )
            if effect and effect in covered_effects and not parameter_variant_commit:
                return ValidationDecision(
                    False,
                    "REDUNDANT_ACTION_EFFECT",
                    candidate_params,
                    f"The global semantic effect {effect} is already covered; choose a still-uncovered TMI-relevant effect or complete the run.",
                )
        if name == "browser.goto":
            supplied_url = str(candidate_params.get("url") or "").strip()
            supplied_origin = _origin(supplied_url)
            if not supplied_origin or supplied_origin not in set(policy.allowed_origins):
                return ValidationDecision(
                    False,
                    "ORIGIN_NOT_ALLOWED",
                    candidate_params,
                    "Navigation URL is outside the configured autonomous-run origin boundary.",
                )
            latest_page = observations.get("latest_page_observation") or {}
            current_url = str(
                observations.get("current_url")
                or (
                    latest_page.get("current_url")
                    if isinstance(latest_page, dict)
                    else ""
                )
                or ""
            ).strip()
            feedback = observations.get("recent_validation_feedback") or {}
            recent_actions = observations.get("recent_actions") or []
            last_action = (
                recent_actions[-1]
                if isinstance(recent_actions, list)
                and recent_actions
                and isinstance(recent_actions[-1], dict)
                else {}
            )
            last_params = (
                last_action.get("params")
                if isinstance(last_action.get("params"), dict)
                else {}
            )
            repeated_reload = (
                last_action.get("name") == "browser.goto"
                and last_action.get("ok") is True
                and str(last_params.get("url") or "").strip() == supplied_url
            )
            if supplied_url == current_url and (feedback or repeated_reload):
                return ValidationDecision(
                    False,
                    "REDUNDANT_NAVIGATION",
                    candidate_params,
                    "The browser is already at this URL; use a selected observation or interaction capability to inspect or change the current state.",
                )
        if name is None:
            missing_actions = tuple(
                action
                for action in policy.agent_required_actions
                if action not in state.successful_actions
            )
            if missing_actions:
                return ValidationDecision(
                    False,
                    "TMI_ACTION_CAPABILITY_INCOMPLETE",
                    candidate_params,
                    "TMI-selected action capabilities not yet exercised successfully: "
                    + ", ".join(missing_actions),
                )
            blockers = autonomous_completion_blockers(runtime_observations or {})
            if blockers:
                return ValidationDecision(
                    False,
                    "AUTONOMOUS_COMPLETION_INCOMPLETE",
                    candidate_params,
                    "Completion audit found agent-observed work still open: "
                    + "; ".join(blockers[:6]),
                )
        return ValidationDecision(
            True,
            "OK",
            candidate_params,
            "Candidate conforms to the autonomous tool-validation boundary.",
        )

def autonomous_completion_blockers(
    observations: Dict[str, Any],
) -> Tuple[str, ...]:
    """Return blockers derived only from the agent's own runtime observations."""
    blockers = []
    ledger = observations.get("semantic_effect_ledger") or {}
    if not isinstance(ledger, dict):
        return ()

    current_url = str(observations.get("current_url") or "").strip()
    if current_url and _current_page_observation_is_stale(observations):
        blockers.append("current page has not been re-observed after its latest navigation or scroll")

    uncovered = ledger.get("uncovered_effect_candidates") or []
    if isinstance(uncovered, list):
        for item in uncovered[:6]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("effect_key") or "unknown effect")
            source = str(item.get("text") or item.get("selector") or "")[:80]
            blockers.append(
                f"uncovered semantic effect {key}"
                + (f" ({source})" if source else "")
            )

    unresolved = ledger.get("unresolved_effect_failures") or []
    if isinstance(unresolved, list):
        for item in unresolved[:3]:
            if not isinstance(item, dict):
                continue
            blockers.append(
                "unresolved semantic effect failure "
                f"{item.get('effect_key') or 'unknown effect'}"
            )

    return tuple(dict.fromkeys(blockers))


def _current_page_activity(observations: Dict[str, Any]) -> Dict[str, Any]:
    current_url = str(observations.get("current_url") or "").strip()
    rows = observations.get("page_activity") or []
    if not current_url or not isinstance(rows, list):
        return {}
    return next(
        (
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("url") or "").strip() == current_url
        ),
        {},
    )


def _current_page_observation_is_stale(observations: Dict[str, Any]) -> bool:
    current_url = str(observations.get("current_url") or "").strip()
    if not current_url:
        return False
    latest = observations.get("latest_page_observation") or {}
    latest_url = (
        str(latest.get("current_url") or "").strip()
        if isinstance(latest, dict)
        else ""
    )
    if latest_url != current_url:
        return True
    return _current_page_activity(observations).get(
        "post_scroll_inspection_required"
    ) is True


def _current_page_has_unobserved_lower_content(
    observations: Dict[str, Any],
) -> bool:
    current_url = str(observations.get("current_url") or "").strip()
    latest = observations.get("latest_page_observation") or {}
    if not current_url or not isinstance(latest, dict):
        return False
    if str(latest.get("current_url") or "").strip() != current_url:
        return False
    viewport = latest.get("viewport") or {}
    return isinstance(viewport, dict) and viewport.get("can_scroll_down") is True
