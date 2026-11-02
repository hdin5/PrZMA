"""Build a public full-trigger configuration from a user-maintained template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_LIMITS = {"max_actions_total": 30, "max_minutes": 15}


def has_content(value: Any) -> bool:
    """Return whether a supplied scenario contains user-authored content."""
    if isinstance(value, dict):
        return any(has_content(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(has_content(item) for item in value)
    return bool(value)


def default_agents(platform: str) -> dict[str, dict[str, Any]]:
    agent_ids = ("A1", "A2") if platform == "discord_web" else ("A1",)
    agents: dict[str, dict[str, Any]] = {}
    for index, agent_id in enumerate(agent_ids, start=1):
        platform_config: dict[str, Any] = {"enabled": True}
        if platform == "discord_web":
            platform_config.update(
                {"login_required": True, "credential_ref": f"DISCORD_{agent_id}"}
            )
        else:
            platform_config["login_required"] = False
        agents[agent_id] = {
            "display_name": f"Agent {index}",
            "persona": "Participate in a safe platform tool-testing scenario.",
            "platforms": {platform: platform_config},
            "browser": {"engine": "chromium"},
        }
    return agents


def valid_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_config(template: dict[str, Any], platform: str) -> dict[str, Any]:
    """Overlay full-trigger settings without changing VM or collection settings."""
    config = dict(template)
    config["purpose"] = "tool_testing"
    config["run_full_trigger"] = True
    config["target_application"] = platform

    rendezvous = config.get("rendezvous")
    if not isinstance(rendezvous, dict):
        rendezvous = {}
    else:
        rendezvous = dict(rendezvous)
    rendezvous["platform"] = platform
    config["rendezvous"] = rendezvous

    agents = config.get("agents")
    if not isinstance(agents, dict) or not agents:
        config["agents"] = default_agents(platform)

    scenario = config.get("scenario")
    if not isinstance(scenario, dict) or not has_content(scenario):
        config["scenario"] = {
            "objective": "Verify that the prepared web service can exchange a short, safe test message."
        }

    run_limits = config.get("run_limits")
    if not isinstance(run_limits, dict):
        run_limits = {}
    else:
        run_limits = dict(run_limits)
    for key, default in DEFAULT_LIMITS.items():
        if not valid_positive_integer(run_limits.get(key)):
            run_limits[key] = default
    config["run_limits"] = run_limits
    return config


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("discord_web", "telegram_web"), required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    try:
        template = json.loads(arguments.template.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"Template config not found: {arguments.template}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"Template config is not valid JSON: {error}") from error
    if not isinstance(template, dict):
        raise SystemExit("Template config must contain a JSON object")

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(build_config(template, arguments.platform), indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
