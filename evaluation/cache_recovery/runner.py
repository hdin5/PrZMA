from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import rpyc
from rpyc.utils.classic import obtain
from dotenv import load_dotenv

from Automation_Agent.agent_generator import vmrun_start
from evaluation.cache_recovery.action_metrics import (
    calculate_action_metrics,
    calculate_posthoc_artifact_efficiency,
)
from evaluation.cache_recovery.autonomous_comparator import compare_autonomous_run
from evaluation.cache_recovery.chromecacheview import parse_cache_csv, run_chromecacheview
from evaluation.cache_recovery.replay import (
    aggregate_paired_action_replays,
    recovery_replay,
    replay_agent_actions,
    select_agent_executions,
    summarize_paired_action_replay,
)
from evaluation.cache_recovery.research_archive.process import (
    ResearchArchiveControlClient,
    ResearchArchiveProcess,
)
from shared.model_client import summarize_usage_records


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "evaluation" / "local" / "evaluation.json"
DEFAULT_RESULTS = ROOT / "evaluation" / "results"
MANUAL_URL = "https://www.nirsoft.net/utils/chrome_cache_view.html"
VM_AGENT_REVISION = "cache-evaluation-profile-v8"


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def autonomous_framework_fingerprint() -> str:
    fixed = [
        ROOT / "Automation_Agent" / "automation_agent.py",
        ROOT / "Automation_Agent" / "semantic_effects.py",
        ROOT / "Automation_Agent" / "tool_validation.py",
        ROOT / "Tool_Manual_Interpreter" / "prompts.py",
        ROOT / "Tool_Manual_Interpreter" / "tmi_core.py",
        ROOT / "Tool_Manual_Interpreter" / "tmi_main.py",
        ROOT / "VM_Agent" / "agent_main.py",
        ROOT / "VM_Agent" / "services" / "browser_service.py",
        ROOT / "shared" / "actions.json",
        ROOT / "shared" / "model_client.py",
        ROOT / "evaluation" / "cache_recovery" / "runner.py",
        ROOT / "evaluation" / "cache_recovery" / "action_metrics.py",
        ROOT / "evaluation" / "cache_recovery" / "autonomous_comparator.py",
        ROOT / "evaluation" / "cache_recovery" / "chromecacheview.py",
    ]
    site_root = ROOT / "evaluation" / "cache_recovery" / "research_archive"
    site_files = [
        path
        for path in site_root.rglob("*")
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
            and "preview_logs" not in path.relative_to(site_root).parts
        )
    ]
    digest = hashlib.sha256()
    for path in sorted({item.resolve() for item in fixed + site_files}, key=str):
        relative = path.relative_to(ROOT.resolve()).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def autonomous_smoke_gate_status(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "a successful autonomous smoke gate does not exist"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"autonomous smoke gate is unreadable: {exc}"
    if record.get("passed") is not True:
        return False, "the latest autonomous smoke did not pass"
    if record.get("framework_fingerprint") != autonomous_framework_fingerprint():
        return False, "autonomous smoke gate does not match the current framework files"
    return True, ""


def autonomous_freeze_gate_status(
    path: Path,
    *,
    expected_fingerprint: str,
) -> tuple[bool, str]:
    if not path.is_file():
        return False, "the explicitly reviewed Campaign freeze manifest does not exist"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"Campaign freeze manifest is unreadable: {exc}"
    if not isinstance(record, dict) or record.get("schema_version") != "1.0.0":
        return False, "Campaign freeze manifest schema_version must be 1.0.0"
    if record.get("user_approved") is not True:
        return False, "Campaign freeze manifest does not record explicit user approval"
    approved_at = str(record.get("approved_at") or "").strip()
    try:
        datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError:
        return False, "Campaign freeze manifest approved_at is missing or invalid"
    if record.get("framework_fingerprint") != expected_fingerprint:
        return False, "Campaign freeze manifest does not match the current framework fingerprint"

    summary_value = str(record.get("pilot_summary_path") or "").strip()
    summary_path = Path(summary_value)
    if not summary_path.is_absolute():
        repository_path = ROOT / summary_path
        manifest_path = path.parent / summary_path
        summary_path = (
            repository_path if repository_path.is_file() else manifest_path
        )
    if not summary_path.is_file():
        return False, "the approved Pilot summary does not exist"
    expected_hash = str(record.get("pilot_summary_sha256") or "").strip().lower()
    if not expected_hash or _sha256_file(summary_path).lower() != expected_hash:
        return False, "the approved Pilot summary hash does not match"
    try:
        summary = _read_json(summary_path)
    except (OSError, ValueError) as exc:
        return False, f"the approved Pilot summary is unreadable: {exc}"
    if summary.get("ready_for_freeze_review") is not True:
        return False, "the approved Pilot summary did not pass freeze-review readiness"
    return True, ""


def build_archive_start_url(base_url: str, run_id: str) -> str:
    parsed = urlsplit(str(base_url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WEB_URL must be an absolute http(s) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/start"):
        path = path + "/start" if path else "/start"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["run_id"] = str(run_id)
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(name: str, ok: bool, detail: str) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def preflight(
    config: Mapping[str, Any],
    *,
    dry_run: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    require_guest_deploy: bool = False,
) -> Dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    vm = config.get("vm") or {}
    site = config.get("site") or {}
    tool = config.get("tool") or {}
    runtime = config.get("runtime") or {}
    checks = []
    external_calls: list[str] = []

    checks.append(_check("OPENAI_API_KEY", bool(env.get("OPENAI_API_KEY")), "set" if env.get("OPENAI_API_KEY") else "missing"))
    checks.append(_check("VM_PASSWORD", bool(env.get("VM_PASSWORD")), "set" if env.get("VM_PASSWORD") else "missing"))
    guest_password_env = str(vm.get("guest_password_env") or "VM_GUEST_PASSWORD")
    if require_guest_deploy or vm.get("deploy_on_pilot", False):
        checks.append(
            _check(
                guest_password_env,
                bool(env.get(guest_password_env)),
                "set" if env.get(guest_password_env) else "missing; required to update VM_Agent inside A1",
            )
        )

    for label, raw in (
        ("vmrun", vm.get("vmrun_path")),
        ("A1 VMX", vm.get("agent_vmx")),
        ("ChromeCacheView", tool.get("exe_path")),
    ):
        path = Path(str(raw or ""))
        checks.append(_check(label, path.is_file(), str(path) if raw else "missing path"))

    try:
        ipaddress.ip_network(str(vm.get("discovery_subnet") or ""), strict=False)
        subnet_ok = True
    except ValueError:
        subnet_ok = False
    checks.append(_check("discovery subnet", subnet_ok, str(vm.get("discovery_subnet") or "missing")))
    try:
        ipaddress.ip_address(str(site.get("host_vmnet_ip") or ""))
        host_ip_ok = True
    except ValueError:
        host_ip_ok = False
    checks.append(_check("host VMnet IP", host_ip_ok, str(site.get("host_vmnet_ip") or "missing")))
    checks.append(_check("model", runtime.get("model") == "gpt-5.6-terra", str(runtime.get("model") or "missing")))
    checks.append(_check("manual URL", tool.get("manual_url") == MANUAL_URL, str(tool.get("manual_url") or "missing")))
    checks.append(_check("tool version", str(tool.get("version") or "") == "2.52", str(tool.get("version") or "missing")))

    exe = Path(str(tool.get("exe_path") or ""))
    expected_hash = str(tool.get("sha256") or "").lower()
    if expected_hash and exe.is_file():
        actual_hash = _sha256_file(exe)
        checks.append(_check("tool SHA-256", actual_hash == expected_hash, actual_hash))

    if not dry_run:
        try:
            external_calls.append("manual_url")
            response = requests.get(str(tool.get("manual_url") or MANUAL_URL), timeout=20)
            response.raise_for_status()
            checks.append(_check("manual reachable", True, f"HTTP {response.status_code}"))
        except Exception as exc:
            checks.append(_check("manual reachable", False, f"{type(exc).__name__}: {str(exc)[:160]}"))

        vmrun_path = Path(str(vm.get("vmrun_path") or ""))
        vmx_path = Path(str(vm.get("agent_vmx") or ""))
        vm_password = env.get(str(vm.get("vm_password_env") or "VM_PASSWORD"), "")
        if vmrun_path.is_file() and vmx_path.is_file() and vm_password:
            external_calls.append("vm_credential_probe")
            try:
                probe_result = subprocess.run(
                    [
                        str(vmrun_path),
                        "-T",
                        "ws",
                        "-vp",
                        vm_password,
                        "getGuestIPAddress",
                        str(vmx_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                probe_text = ((probe_result.stderr or "") + "\n" + (probe_result.stdout or "")).strip()
                password_error = any(
                    token in probe_text.lower()
                    for token in ("incorrect password", "invalid password", "cannot decrypt")
                )
                access_ok = probe_result.returncode == 0 or (
                    not password_error and "not powered on" in probe_text.lower()
                )
                detail = "credential accepted" if access_ok else probe_text[:200]
                checks.append(_check("VM encryption password", access_ok, detail))
            except Exception as exc:
                checks.append(_check("VM encryption password", False, f"{type(exc).__name__}: {str(exc)[:160]}"))

        port = int(site.get("port") or 8765)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((str(site.get("bind_host") or "0.0.0.0"), port))
            checks.append(_check("site port", True, str(port)))
        except OSError as exc:
            checks.append(_check("site port", False, f"{port}: {exc}"))
        finally:
            probe.close()

    return {
        "schema_version": "1.0.0",
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "external_calls": external_calls,
    }


def build_autonomous_run_config(
    interpreted: Mapping[str, Any],
    local: Mapping[str, Any],
    campaign_id: str,
    run_id: str,
    web_url: str,
) -> Dict[str, Any]:
    """Add runtime boundaries without replacing the TMI-selected contract."""
    config: Dict[str, Any] = copy.deepcopy(dict(interpreted))
    vm = local.get("vm") or {}
    site = local.get("site") or {}
    parsed = urlsplit(str(web_url).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web_url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("web_url must not contain embedded credentials")
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port = parsed.port or default_port
    origin = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port}"
    host_ip = str(site.get("host_vmnet_ip") or "").strip()
    if not host_ip:
        raise ValueError("site.host_vmnet_ip is required")

    contract = config.get("tool_validation")
    if not isinstance(contract, dict):
        raise ValueError("interpreted config is missing tool_validation")
    source_contract = copy.deepcopy(contract)

    profile = rf"C:\PrZMA\evaluation_profiles\{campaign_id}\{run_id}"
    agents = config.setdefault("agents", {})
    if "A1" not in agents:
        raise ValueError("autonomous cache recovery requires interpreted agent A1")
    for extra_agent in list(agents):
        if extra_agent != "A1":
            del agents[extra_agent]
    browser = agents["A1"].setdefault("browser", {})
    browser.update(
        {
            "engine": "chromium",
            "channel": "chrome",
            "headless": False,
            "user_data_dir": profile,
            "profile_name": "Default",
            "require_fresh_profile": True,
            "locale": "en-US",
            "timezone": "UTC",
            "extra_args": [
                f"--host-resolver-rules=MAP {parsed.hostname} {host_ip}",
                "--no-proxy-server",
                "--disable-background-networking",
            ],
        }
    )

    config["tool_validation"] = source_contract
    config["available_resources"] = {"web_url": str(web_url).strip()}
    config["evaluation"] = {
        "mode": "autonomous_cache_recovery",
        "allowed_origins": [origin],
        "max_validation_repairs": int(site.get("max_validation_repairs") or 5),
    }
    snapshot = config.setdefault("snapshot", {})
    event_trigger = snapshot.setdefault("event_trigger", {})
    source_snapshot_triggers = [
        str(value)
        for value in event_trigger.get("on_actions") or []
        if str(value).strip()
    ]
    final_snapshot_triggers = list(dict.fromkeys(source_snapshot_triggers + ["browser.close"]))
    event_trigger["enabled"] = True
    event_trigger["on_actions"] = final_snapshot_triggers
    event_trigger.setdefault("cooldown_sec", 0)
    event_trigger.setdefault(
        "conditions",
        [
            {"left": "agent_id", "op": "eq", "right": "A1"},
            {"left": "result.ok", "op": "eq", "right": True},
        ],
    )
    config["evaluation"]["snapshot_trigger_adapter"] = {
        "source_trigger_actions": source_snapshot_triggers,
        "added_system_trigger": "browser.close",
        "reason": "collect the stable post-cleanup browser profile after cache flush",
        "agent_action_contract_changed": False,
    }
    config["run_limits"] = {
        "max_actions_total": int(site.get("max_actions_total") or 100),
        "max_minutes": int(site.get("max_minutes") or 30),
    }
    config["vm_boot"] = {
        "enabled": True,
        "provider": "vmware",
        "gui": bool(vm.get("gui", False)),
        "vmrun_path": str(vm.get("vmrun_path")),
        "vmx_paths": {"A1": str(vm.get("agent_vmx"))},
        "vm_password_env": str(vm.get("vm_password_env") or "VM_PASSWORD"),
        "wait_timeout_sec": int(vm.get("wait_timeout_sec") or 240),
        "poll_interval_sec": 2.0,
    }
    config["discovery"] = {
        "rpyc_port": int(vm.get("rpyc_port") or 18861),
        "subnet": str(vm.get("discovery_subnet")),
    }
    return config


def _running_vms(vmrun_path: Path) -> set[str]:
    result = subprocess.run(
        [str(vmrun_path), "-T", "ws", "list"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return set()
    return {line.strip().lower() for line in result.stdout.splitlines()[1:] if line.strip()}


def build_vmrun_guest_command(
    vmrun_path: Path,
    vmx_path: Path,
    vm_password: str,
    guest_username: str,
    guest_password: str,
    operation: list[str],
) -> list[str]:
    if not operation:
        raise ValueError("VMware guest operation is required")
    return [
        str(Path(vmrun_path)),
        "-T",
        "ws",
        "-vp",
        vm_password,
        "-gu",
        guest_username,
        "-gp",
        guest_password,
        operation[0],
        str(Path(vmx_path)),
        *operation[1:],
    ]


def _guest_operation(
    config: Mapping[str, Any],
    environ: Mapping[str, str],
    operation: list[str],
    *,
    timeout_sec: int = 120,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    vm = config.get("vm") or {}
    vm_password = environ.get(str(vm.get("vm_password_env") or "VM_PASSWORD"), "")
    guest_username = str(vm.get("guest_username") or "VM Agent")
    guest_password_env = str(vm.get("guest_password_env") or "VM_GUEST_PASSWORD")
    guest_password = environ.get(guest_password_env, "")
    if not vm_password:
        raise RuntimeError("Missing VM encryption password")
    if not guest_password:
        raise RuntimeError(f"Missing Windows guest password in {guest_password_env}")
    command = build_vmrun_guest_command(
        Path(str(vm.get("vmrun_path"))),
        Path(str(vm.get("agent_vmx"))),
        vm_password,
        guest_username,
        guest_password,
        operation,
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if result.returncode != 0 and not allow_failure:
        message = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[:500]
        raise RuntimeError(f"VMware guest operation failed ({operation[0]}): {message}")
    return result


def _guest_ip(config: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    vm = config.get("vm") or {}
    command = [
        str(vm.get("vmrun_path")),
        "-T",
        "ws",
        "-vp",
        environ.get(str(vm.get("vm_password_env") or "VM_PASSWORD"), ""),
        "getGuestIPAddress",
        str(vm.get("agent_vmx")),
        "-wait",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        message = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[:500]
        raise RuntimeError(f"Could not obtain A1 guest IP: {message}")
    return result.stdout.strip().splitlines()[-1].strip()


def validate_vm_agent_revision(info: Mapping[str, Any]) -> Dict[str, Any]:
    value = dict(info)
    revision = value.get("vm_agent_revision")
    if revision != VM_AGENT_REVISION:
        raise RuntimeError(f"Unexpected VM_Agent revision: {revision!r}; expected {VM_AGENT_REVISION!r}")
    if value.get("profile_tracking") is not True:
        raise RuntimeError("VM_Agent revision does not report profile tracking")
    return value


def connect_vm_agent(ip: str, port: int, *, timeout_sec: int = 90) -> rpyc.Connection:
    deadline = time.monotonic() + timeout_sec
    last_error = "Agent did not respond"
    while time.monotonic() < deadline:
        try:
            connection = rpyc.connect(
                ip,
                int(port),
                config={"sync_request_timeout": 180, "allow_public_attrs": True},
            )
            try:
                validate_vm_agent_revision(dict(obtain(connection.root.runtime_info())))
            except Exception:
                connection.close()
                raise
            return connection
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            time.sleep(1)
    raise RuntimeError(f"VM_Agent connection timeout at {ip}:{port}: {last_error}")


def provision_vm_agent(config: Mapping[str, Any], environ: Mapping[str, str]) -> Dict[str, Any]:
    vm = config.get("vm") or {}
    if not vm.get("deploy_on_pilot", False):
        return {"deployed": False}
    started = time.monotonic()
    print("[VM] Disabling guest sleep and display timeouts for the evaluation...", flush=True)
    powercfg = r"C:\Windows\System32\powercfg.exe"
    for timeout_name in (
        "standby-timeout-ac",
        "hibernate-timeout-ac",
        "monitor-timeout-ac",
    ):
        _guest_operation(
            config,
            environ,
            ["runProgramInGuest", powercfg, "/change", timeout_name, "0"],
        )
    print("[VM] Stopping the guest Agent task before deployment...", flush=True)
    guest_root = str(vm.get("guest_agent_root") or r"C:\Users\VM Agent\VM_Agent").rstrip("\\")
    schtasks = r"C:\Windows\System32\schtasks.exe"
    _guest_operation(
        config,
        environ,
        ["runProgramInGuest", schtasks, "/End", "/TN", "PrZMA_VM_Agent"],
        allow_failure=True,
    )
    deployments = (
        (ROOT / "VM_Agent" / "agent_main.py", guest_root + r"\agent_main.py"),
        (
            ROOT / "VM_Agent" / "services" / "browser_service.py",
            guest_root + r"\services\browser_service.py",
        ),
    )
    for source, destination in deployments:
        print(f"[VM] Deploying {source.relative_to(ROOT)}", flush=True)
        _guest_operation(
            config,
            environ,
            ["copyFileFromHostToGuest", str(source.resolve()), destination],
        )
    _guest_operation(
        config,
        environ,
        ["runProgramInGuest", schtasks, "/Run", "/TN", "PrZMA_VM_Agent"],
    )

    ip = _guest_ip(config, environ)
    print(f"[VM] Guest address: {ip}; waiting for Agent revision {VM_AGENT_REVISION}", flush=True)
    port = int(vm.get("rpyc_port") or 18861)
    wait_timeout = int(vm.get("wait_timeout_sec") or 240)
    deadline = time.time() + wait_timeout
    last_error = "VM_Agent did not respond"
    while time.time() < deadline:
        try:
            connection = rpyc.connect(
                ip,
                port,
                config={"sync_request_timeout": 180, "allow_public_attrs": True},
            )
            try:
                info = validate_vm_agent_revision(dict(obtain(connection.root.runtime_info())))
            finally:
                connection.close()
            print(f"[VM] Agent ready after {time.monotonic() - started:.1f}s", flush=True)
            return {"deployed": True, "guest_ip": ip, "port": port, "runtime_info": info}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        time.sleep(2)
    raise RuntimeError(f"Updated VM_Agent did not become ready: {last_error}")


def ensure_vm_started(config: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    vm = config.get("vm") or {}
    vmrun = Path(str(vm.get("vmrun_path")))
    vmx = Path(str(vm.get("agent_vmx")))
    if str(vmx.resolve()).lower() in _running_vms(vmrun):
        return
    password = environ.get(str(vm.get("vm_password_env") or "VM_PASSWORD"))
    result = vmrun_start(str(vmrun), str(vmx), gui=bool(vm.get("gui", False)), vm_password=password)
    if result.returncode != 0:
        message = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[:500]
        raise RuntimeError(f"A1 VM start failed: {message}")


def _run_tmi(
    run_id: str,
    out_dir: Path,
    local: Mapping[str, Any],
    *,
    web_url: Optional[str] = None,
) -> Path:
    tool = local.get("tool") or {}
    command = [
        sys.executable,
        "-m",
        "Tool_Manual_Interpreter.tmi_main",
        "--tool-name",
        "ChromeCacheView",
        "--tool-version",
        str(tool.get("version") or "2.52"),
        "--manual-url",
        str(tool.get("manual_url") or MANUAL_URL),
        "--run-id",
        run_id,
        "--out-dir",
        str(out_dir),
        "--actions",
        str(ROOT / "shared" / "actions.json"),
        "--catalog",
        str(ROOT / "Snapshot_Engine" / "artifact_catalog.json"),
        "--template-config",
        str(ROOT / "Config(templete).json"),
    ]
    environment = os.environ.copy()
    runtime = local.get("runtime") or {}
    if runtime.get("model"):
        environment["OPENAI_MODEL"] = str(runtime["model"])
    if runtime.get("reasoning_effort"):
        environment["OPENAI_REASONING_EFFORT"] = str(runtime["reasoning_effort"])
    if web_url:
        environment["WEB_URL"] = str(web_url)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"TMI failed with return code {result.returncode}")
    interpreted = out_dir / "interpreted_przma_config.json"
    if not interpreted.is_file():
        raise RuntimeError(f"TMI did not create {interpreted}")
    return interpreted


def _load_jsonl(path: Path) -> list[Dict[str, Any]]:
    values = []
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
        except json.JSONDecodeError:
            continue
    return values


def _snapshot_bundle(
    przma_run_dir: Path,
    destination: Path,
    *,
    required_artifact_key: Optional[str] = None,
    required_trigger_action: Optional[str] = None,
) -> tuple[Path, Dict[str, Any]]:
    candidates = sorted((przma_run_dir / "snapshots").glob("snap_*"))
    if not candidates:
        raise RuntimeError("No snapshot directory was produced")
    valid: list[tuple[Path, Dict[str, Any]]] = []
    for path in candidates:
        if not (path / "snapshot.zip").is_file() or not (path / "manifest.json").is_file():
            continue
        manifest = _read_json(path / "manifest.json")
        if required_artifact_key:
            artifacts = manifest.get("artifacts") or []
            artifact_keys = {
                str((item.get("meta") or {}).get("artifact_key") or item.get("artifact_key") or "")
                for item in artifacts
                if isinstance(item, Mapping)
            }
            if required_artifact_key not in artifact_keys:
                continue
        if required_trigger_action:
            trigger = manifest.get("trigger") if isinstance(manifest.get("trigger"), Mapping) else {}
            if str(trigger.get("action_name") or "") != required_trigger_action:
                continue
        valid.append((path, manifest))
    if not valid:
        requirements = ", ".join(
            value
            for value in (
                f"artifact={required_artifact_key}" if required_artifact_key else "",
                f"trigger={required_trigger_action}" if required_trigger_action else "",
            )
            if value
        )
        suffix = f" matching {requirements}" if requirements else ""
        raise RuntimeError(f"No complete snapshot was found{suffix}")

    def created_key(item: tuple[Path, Dict[str, Any]]) -> tuple[str, float]:
        path, manifest = item
        return str(manifest.get("created_at") or ""), path.stat().st_mtime

    source, source_manifest = max(valid, key=created_key)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(source / "snapshot.zip") as archive:
        archive.extractall(destination)
    return destination, source_manifest


def run_autonomous_once(
    local: Mapping[str, Any],
    campaign_id: str,
    run_id: str,
    *,
    archive_client: ResearchArchiveControlClient,
    vm_connection_info: Mapping[str, Any],
    web_url_base: str,
) -> Dict[str, Any]:
    end_to_end_started = time.monotonic()
    end_to_end_started_at = datetime.now(timezone.utc).isoformat()
    results_root = Path(str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS))
    run_dir = results_root / f"run_{run_id}"
    if run_dir.exists():
        raise FileExistsError(f"Run output already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    web_url = build_archive_start_url(web_url_base, run_id)
    archive_client.register_run(run_id)
    _write_json(
        run_dir / "vm_connection.json",
        {
            "guest_ip": vm_connection_info.get("guest_ip"),
            "port": vm_connection_info.get("port"),
            "runtime_info": vm_connection_info.get("runtime_info"),
        },
    )

    tmi_dir = run_dir / "tmi"
    interpreted_path = _run_tmi(run_id, tmi_dir, local, web_url=web_url)
    interpreted = _read_json(interpreted_path)
    run_config = build_autonomous_run_config(
        interpreted,
        local,
        campaign_id,
        run_id,
        web_url,
    )
    run_config["vm_boot"]["enabled"] = False
    config_path = run_dir / "interpreted_przma_config.json"
    _write_json(config_path, run_config)

    przma_out = run_dir / "przma"
    command = [
        sys.executable,
        str(ROOT / "main.py"),
        "--config",
        str(config_path),
        "--run-id",
        run_id,
        "--rules",
        str(tmi_dir / "tmi_rules.json"),
        "--catalog",
        str(ROOT / "Snapshot_Engine" / "artifact_catalog.json"),
        "--out-dir",
        str(przma_out),
        "--max-steps",
        str((run_config.get("run_limits") or {}).get("max_actions_total") or 40),
    ]
    with (run_dir / "przma.stdout.log").open("w", encoding="utf-8") as stdout, (
        run_dir / "przma.stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        agent_started = time.monotonic()
        agent_started_at = datetime.now(timezone.utc).isoformat()
        process = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            check=False,
            timeout=1800,
        )
        agent_finished_at = datetime.now(timezone.utc).isoformat()
        agent_execution_seconds = time.monotonic() - agent_started
    site_status = archive_client.status(run_id)
    _write_json(run_dir / "site_status.json", site_status)
    if process.returncode != 0:
        raise RuntimeError(f"PrZMA run failed with return code {process.returncode}; see {run_dir}")

    przma_run_dir = run_dir / "przma" / f"run_{run_id}"
    action_path = przma_run_dir / "actions.jsonl"
    actions = _load_jsonl(action_path)
    action_metrics = calculate_action_metrics(
        actions,
        run_config.get("tool_validation") or {},
        evaluation_mode="autonomous_cache_recovery",
    )
    _write_json(run_dir / "action_metrics.json", action_metrics)

    unpacked, snapshot_manifest = _snapshot_bundle(
        przma_run_dir,
        run_dir / "snapshot",
        required_artifact_key="chromium.cache.cache_data",
        required_trigger_action="browser.close",
    )
    _write_json(run_dir / "snapshot_manifest.json", snapshot_manifest)
    cache_dirs = [path for path in unpacked.rglob("chromium.cache.cache_data") if path.is_dir()]
    if not cache_dirs:
        raise RuntimeError("Snapshot does not contain chromium.cache.cache_data")
    cache_dir = cache_dirs[0]
    tool = local.get("tool") or {}
    csv_path = run_dir / "chromecacheview.csv"
    extracted = run_dir / "extracted_cache"
    parsed_web = urlsplit(web_url)
    default_port = 80 if parsed_web.scheme.lower() == "http" else 443
    web_port = parsed_web.port or default_port
    base_origin = f"{parsed_web.scheme.lower()}://{parsed_web.hostname.lower()}:{web_port}"
    tool_run = run_chromecacheview(
        Path(str(tool.get("exe_path"))),
        cache_dir,
        csv_path,
        extracted,
        url_prefix=base_origin,
    )
    _write_json(run_dir / "tool_run.json", tool_run.__dict__)
    records = parse_cache_csv(csv_path)
    requests_log = _load_jsonl(run_dir / "archive_requests.jsonl")
    action_metrics.update(
        calculate_posthoc_artifact_efficiency(actions, requests_log)
    )
    _write_json(run_dir / "action_metrics.json", action_metrics)
    resource_manifest = _read_json(
        ROOT / "evaluation" / "cache_recovery" / "research_archive" / "resource_manifest.json"
    )
    behavior_manifest = _read_json(
        ROOT / "evaluation" / "cache_recovery" / "research_archive" / "behavior_manifest.json"
    )
    comparison = compare_autonomous_run(
        resource_manifest,
        behavior_manifest,
        requests_log,
        snapshot_manifest,
        records,
        extracted,
        base_origin=base_origin,
        size_semantics=tool.get("size_semantics") or None,
        content_semantics=tool.get("content_semantics") or None,
    )
    _write_json(run_dir / "comparison.json", comparison)
    agent_summary_path = action_path.with_suffix(".summary.json")
    agent_summary = _read_json(agent_summary_path) if agent_summary_path.is_file() else {}
    model_usage = summarize_usage_records(
        _load_jsonl(tmi_dir / "model_usage.jsonl")
        + _load_jsonl(przma_run_dir / "model_usage.jsonl")
    )
    _write_json(run_dir / "model_usage_summary.json", model_usage)
    timing = {
        "end_to_end_started_at": end_to_end_started_at,
        "agent_started_at": agent_started_at,
        "agent_finished_at": agent_finished_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "agent_execution_seconds": round(agent_execution_seconds, 3),
        "end_to_end_seconds": round(time.monotonic() - end_to_end_started, 3),
    }
    result = {
        "run_id": run_id,
        "evaluation_mode": "autonomous_cache_recovery",
        "web_origin": base_origin,
        "site_status": site_status,
        "action_metrics": action_metrics,
        "agent_summary": agent_summary,
        "model_usage": model_usage,
        "timing": timing,
        "comparison": comparison["summary"],
    }
    _write_json(run_dir / "run_manifest.json", result)
    return result


def run_action_replay_once(
    local: Mapping[str, Any],
    source_run_dir: Path,
    replay_run_id: str,
    *,
    archive_client: ResearchArchiveControlClient,
    connection: rpyc.Connection,
    web_url_base: str,
) -> Dict[str, Any]:
    source_run_dir = Path(source_run_dir)
    source_manifest = _read_json(source_run_dir / "run_manifest.json")
    source_run_id = str(source_manifest.get("run_id") or "").strip()
    if not source_run_id:
        raise ValueError("Source run manifest is missing run_id")
    source_action_paths = sorted((source_run_dir / "przma").rglob("actions.jsonl"))
    if not source_action_paths:
        raise FileNotFoundError("Source run does not contain actions.jsonl")
    source_actions = _load_jsonl(source_action_paths[-1])

    results_root = Path(str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS))
    replay_dir = results_root / f"run_{replay_run_id}"
    if replay_dir.exists():
        raise FileExistsError(f"Action replay output already exists: {replay_dir}")
    replay_dir.mkdir(parents=True)
    archive_client.register_run(replay_run_id)

    new_web_url = build_archive_start_url(web_url_base, replay_run_id)
    old_web_url = str(source_manifest.get("web_url") or "").strip()
    if not old_web_url:
        source_config_path = source_run_dir / "interpreted_przma_config.json"
        if source_config_path.is_file():
            old_web_url = str(
                (_read_json(source_config_path).get("available_resources") or {}).get("web_url")
                or ""
            ).strip()

    adjusted_actions, adjustments = _prepare_action_replay_entries(
        source_actions,
        old_web_url=old_web_url,
        new_web_url=new_web_url,
    )

    parsed = urlsplit(new_web_url)
    site = local.get("site") or {}
    host_ip = str(site.get("host_vmnet_ip") or "")
    profile = rf"C:\PrZMA\evaluation_profiles\replay\{replay_run_id}"
    launch_request = {
        "schema_version": "1.0.0",
        "run_id": replay_run_id,
        "agent_id": "A1",
        "action_id": "replay_bootstrap",
        "name": "browser.launch",
        "params": {
            "browser_config": {
                "browser": "chromium",
                "channel": "chrome",
                "headless": False,
                "user_data_dir": profile,
                "profile_name": "Default",
                "require_fresh_profile": True,
                "locale": "en-US",
                "timezone": "UTC",
                "extra_args": [
                    f"--host-resolver-rules=MAP {parsed.hostname} {host_ip}",
                    "--no-proxy-server",
                    "--disable-background-networking",
                ],
            }
        },
    }

    def execute(request: Dict[str, Any]) -> Mapping[str, Any]:
        return obtain(connection.root.execute_action(json.dumps(request, ensure_ascii=False)))

    bootstrap_result = dict(execute(launch_request))
    if bootstrap_result.get("ok") is not True:
        raise RuntimeError(f"Action replay browser launch failed: {bootstrap_result.get('error')}")
    replay_results = replay_agent_actions(
        adjusted_actions,
        replay_run_id=replay_run_id,
        execute=execute,
    )
    cleanup_request = {
        "schema_version": "1.0.0",
        "run_id": replay_run_id,
        "agent_id": "A1",
        "action_id": "replay_cleanup",
        "name": "browser.close",
        "params": {},
    }
    cleanup_result = dict(execute(cleanup_request))

    replay_log = replay_dir / "replay_actions.jsonl"
    for entry in replay_results:
        with replay_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    site_status = archive_client.status(replay_run_id)
    _write_json(replay_dir / "site_status.json", site_status)

    resources = _read_json(
        ROOT / "evaluation" / "cache_recovery" / "research_archive" / "resource_manifest.json"
    ).get("resources") or []
    interactive_keys = {
        (str(item.get("resource_id") or ""), str(item.get("variant_id") or "default"))
        for item in resources
        if isinstance(item, Mapping)
        and item.get("cache_eligible") is True
        and item.get("classification") == "interactive"
    }
    request_events = _load_jsonl(replay_dir / "archive_requests.jsonl")
    generated_keys = {
        (str(item.get("resource_id") or ""), str(item.get("variant_id") or "default"))
        for item in request_events
    }
    generated_interactive = interactive_keys.intersection(generated_keys)
    interactive_resource_ids = {resource_id for resource_id, _variant_id in interactive_keys}
    paired_metrics = summarize_paired_action_replay(
        source_actions=source_actions,
        replay_results=replay_results,
        source_requests=_load_jsonl(source_run_dir / "archive_requests.jsonl"),
        replay_requests=request_events,
        interactive_resource_ids=interactive_resource_ids,
    )
    manifest = {
        "schema_version": "1.0.0",
        "replay_type": "action",
        "source_run_id": source_run_id,
        "run_id": replay_run_id,
        "web_url": new_web_url,
        "profile": profile,
        "runtime_context_adjustments": adjustments,
        "interactive_target_count": len(interactive_keys),
        "generated_interactive_count": len(generated_interactive),
        "generation_coverage": (
            len(generated_interactive) / len(interactive_keys) if interactive_keys else 0.0
        ),
        "bootstrap_result": bootstrap_result,
        "cleanup_result": cleanup_result,
        "metric_scope": "paired recorded-action replay",
        "excluded_from_live_run_metrics": True,
    }
    manifest.update(paired_metrics)
    _write_json(replay_dir / "replay_manifest.json", manifest)
    return manifest


def _prepare_action_replay_entries(
    source_actions: Iterable[Mapping[str, Any]],
    *,
    old_web_url: str,
    new_web_url: str,
) -> tuple[list[Dict[str, Any]], list[Dict[str, str]]]:
    adjusted_actions = copy.deepcopy(list(source_actions))
    adjustments: list[Dict[str, str]] = []
    for copied in adjusted_actions:
        action = copied.get("action") or {}
        params = action.get("params") or {}
        if (
            copied.get("kind") == "action"
            and copied.get("origin") == "agent"
            and action.get("name") == "browser.goto"
            and old_web_url
            and str(params.get("url") or "").strip() == old_web_url
        ):
            params["url"] = new_web_url
            adjustments.append(
                {
                    "action": "browser.goto",
                    "field": "url",
                    "reason": "replace run-scoped session bootstrap URL",
                }
            )
    return adjusted_actions, adjustments


def _load_local_config(path: Path) -> Dict[str, Any]:
    load_dotenv(ROOT / ".env")
    config = _read_json(path)
    runtime = config.setdefault("runtime", {})
    runtime.setdefault("model", os.getenv("OPENAI_MODEL", "gpt-5.6-terra"))
    return config


def validate_research_archive_foundation(timeout_sec: int = 240) -> Dict[str, Any]:
    modules = [
        "evaluation.cache_recovery.verification.test_manifest",
        "evaluation.cache_recovery.verification.test_server",
        "evaluation.cache_recovery.verification.test_assets",
        "evaluation.cache_recovery.verification.test_chromium",
        "evaluation.cache_recovery.verification.test_process",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *modules, "-v"],
        cwd=ROOT,
        check=False,
        timeout=timeout_sec,
    )
    return {"ok": result.returncode == 0, "returncode": result.returncode}


def autonomous_smoke_gate_passes(result: Mapping[str, Any]) -> tuple[bool, list[str]]:
    comparison = result.get("comparison") if isinstance(result.get("comparison"), Mapping) else {}
    agent_summary = (
        result.get("agent_summary") if isinstance(result.get("agent_summary"), Mapping) else {}
    )
    reasons = []
    agent_failures = (
        agent_summary.get("agent_failures")
        if isinstance(agent_summary.get("agent_failures"), Mapping)
        else {}
    )
    infrastructure_codes = sorted(
        {
            str(item.get("code") or "unspecified")
            for item in agent_failures.values()
            if isinstance(item, Mapping)
            and item.get("category") == "infrastructure_failure"
        }
    )
    if infrastructure_codes:
        reasons.extend(f"infrastructure failure: {code}" for code in infrastructure_codes)
    elif agent_summary.get("autonomous_completion") is not True:
        reasons.append("Agent did not complete autonomously")
    if comparison.get("snapshot_present") is not True:
        reasons.append("snapshot was not collected")
    if int(comparison.get("generated_interactive_count") or 0) <= 0:
        reasons.append("no interactive resource was generated")
    if int(comparison.get("e2e_matched_interactive_count") or 0) <= 0:
        reasons.append("no interactive resource completed E2E recovery")
    if float(comparison.get("autonomous_e2e_artifact_coverage") or 0.0) <= 0.0:
        reasons.append("Autonomous E2E Artifact Coverage is zero")
    interactive_target_count = int(comparison.get("interactive_target_count") or 0)
    e2e_matched_count = int(comparison.get("e2e_matched_interactive_count") or 0)
    if (
        interactive_target_count > 0
        and e2e_matched_count < max(1, interactive_target_count - 1)
    ):
        reasons.append("interactive family coverage is below the smoke threshold")
    behavior_count = int(comparison.get("behavior_count") or 0)
    verified_behavior_count = int(comparison.get("verified_behavior_count") or 0)
    if behavior_count > 0 and verified_behavior_count < max(1, behavior_count - 1):
        reasons.append("behavior coverage is below the smoke threshold")
    if int(comparison.get("generated_interactive_count") or 0) > 0:
        if float(comparison.get("recovery_recall") or 0.0) < 0.98:
            reasons.append("recovery recall is below 98%")
        if float(comparison.get("parsing_precision") or 0.0) < 0.98:
            reasons.append("parsing precision is below 98%")
    field_accuracy = (
        comparison.get("field_accuracy")
        if isinstance(comparison.get("field_accuracy"), Mapping)
        else {}
    )
    if int(comparison.get("field_accuracy_denominator") or 0) > 0 and any(
        float(value or 0.0) < 0.98 for value in field_accuracy.values()
    ):
        reasons.append("field accuracy is below 98%")
    return not reasons, reasons


def _numeric_summary(values: Iterable[float]) -> Dict[str, float]:
    rows = [float(value) for value in values]
    if not rows:
        return {"mean": 0.0, "minimum": 0.0, "maximum": 0.0}
    return {
        "mean": sum(rows) / len(rows),
        "minimum": min(rows),
        "maximum": max(rows),
    }


def resolve_autonomous_run_count(mode: str, requested: Optional[int]) -> int:
    if mode == "autonomous-smoke":
        return 1
    if mode == "autonomous-pilot":
        return max(1, int(3 if requested is None else requested))
    if mode == "autonomous-campaign":
        return 20
    raise ValueError(f"Unsupported autonomous mode: {mode}")


def classify_autonomous_attempt_failure(
    run_dir: Path,
    exc: Exception,
) -> Dict[str, Any]:
    usage_records = _load_jsonl(run_dir / "tmi" / "model_usage.jsonl")
    failed_calls = [
        item for item in usage_records if str(item.get("status") or "") == "error"
    ]
    latest = failed_calls[-1] if failed_calls else {}
    provider_code = str(latest.get("provider_error_code") or "").strip()
    http_status = latest.get("http_status")
    if provider_code == "insufficient_quota":
        classification = "infrastructure_failure"
        code = "provider_insufficient_quota"
    elif http_status == 429:
        classification = "infrastructure_failure"
        code = "provider_rate_limit_or_quota"
    elif isinstance(http_status, int) and http_status >= 500:
        classification = "infrastructure_failure"
        code = "provider_server_error"
    else:
        classification = "execution_failure"
        code = "autonomous_attempt_failed"
    return {
        "classification": classification,
        "code": code,
        "stage": str(latest.get("stage") or "unknown"),
        "http_status": http_status,
        "exception_type": type(exc).__name__,
    }


def summarize_autonomous_pilot(
    campaign_id: str,
    results: Iterable[Mapping[str, Any]],
    *,
    required_valid_runs: int = 5,
    readiness_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    attempts = list(results)
    valid: list[Mapping[str, Any]] = []
    invalid_runs = []
    for result in attempts:
        passed, reasons = autonomous_smoke_gate_passes(result)
        if passed:
            valid.append(result)
            continue
        agent_summary = (
            result.get("agent_summary")
            if isinstance(result.get("agent_summary"), Mapping)
            else {}
        )
        invalid_runs.append(
            {
                "run_id": str(result.get("run_id") or ""),
                "classification": str(
                    agent_summary.get("stop_reason") or "invalid_autonomous_result"
                ),
                "reasons": reasons,
            }
        )

    comparisons = [
        item.get("comparison") if isinstance(item.get("comparison"), Mapping) else {}
        for item in valid
    ]
    action_metrics = [
        item.get("action_metrics")
        if isinstance(item.get("action_metrics"), Mapping)
        else {}
        for item in valid
    ]
    usage_rows = [
        item.get("model_usage") if isinstance(item.get("model_usage"), Mapping) else {}
        for item in valid
    ]
    timing_rows = [
        item.get("timing") if isinstance(item.get("timing"), Mapping) else {}
        for item in valid
    ]

    coverage = _numeric_summary(
        float(item.get("autonomous_e2e_artifact_coverage") or 0.0)
        for item in comparisons
    )
    coverage.update(
        {
            "matched_total": sum(
                int(item.get("e2e_matched_interactive_count") or 0)
                for item in comparisons
            ),
            "target_total": sum(
                int(item.get("interactive_target_count") or 0)
                for item in comparisons
            ),
        }
    )
    recovery = _numeric_summary(
        float(item.get("recovery_recall") or 0.0) for item in comparisons
    )
    precision = _numeric_summary(
        float(item.get("parsing_precision") or 0.0) for item in comparisons
    )
    behavior = _numeric_summary(
        float(item.get("behavior_coverage") or 0.0) for item in comparisons
    )
    behavior_verified_total = sum(
        int(item.get("verified_behavior_count") or 0) for item in comparisons
    )
    behavior_target_total = sum(
        int(item.get("behavior_count") or 0) for item in comparisons
    )
    behavior["verified_total"] = behavior_verified_total
    behavior["target_total"] = behavior_target_total
    behavior["pooled"] = (
        behavior_verified_total / behavior_target_total
        if behavior_target_total
        else 0.0
    )

    field_names = sorted(
        {
            str(name)
            for item in comparisons
            for name in (
                item.get("field_accuracy")
                if isinstance(item.get("field_accuracy"), Mapping)
                else {}
            )
        }
    )
    field_accuracy = {
        name: _numeric_summary(
            float((item.get("field_accuracy") or {}).get(name) or 0.0)
            for item in comparisons
        )
        for name in field_names
    }

    action_types = sorted(
        {
            str(name)
            for item in action_metrics
            for name in (item.get("distinct_autonomous_action_types") or [])
        }
    )
    actions = {
        "executed": _numeric_summary(
            float(item.get("autonomous_executed_action_count") or 0)
            for item in action_metrics
        ),
        "success_rate": _numeric_summary(
            float(item.get("autonomous_action_success_rate") or 0.0)
            for item in action_metrics
        ),
        "distinct_action_types": action_types,
        "parameter_variants": _numeric_summary(
            float(item.get("distinct_proposed_parameter_variants") or 0)
            for item in action_metrics
        ),
        "actions_after_last_new_target": _numeric_summary(
            float(item.get("actions_after_last_new_target") or 0)
            for item in action_metrics
        ),
        "duplicate_navigation_count": sum(
            int(item.get("duplicate_navigation_count") or 0)
            for item in action_metrics
        ),
        "longest_identical_signature_streak": max(
            (
                int(item.get("longest_identical_signature_streak") or 0)
                for item in action_metrics
            ),
            default=0,
        ),
        "longest_identical_proposal_signature_streak": max(
            (
                int(
                    item.get("longest_identical_proposal_signature_streak")
                    if item.get("longest_identical_proposal_signature_streak") is not None
                    else item.get("longest_identical_signature_streak") or 0
                )
                for item in action_metrics
            ),
            default=0,
        ),
        "longest_identical_executed_signature_streak": max(
            (
                int(
                    item.get("longest_identical_executed_signature_streak")
                    if item.get("longest_identical_executed_signature_streak") is not None
                    else item.get("longest_identical_signature_streak") or 0
                )
                for item in action_metrics
            ),
            default=0,
        ),
    }
    model_usage = {
        key: sum(float(item.get(key) or 0) for item in usage_rows)
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "estimated_cost_usd",
        )
    }
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        model_usage[key] = int(model_usage[key])
    valid_count = len(valid)
    model_usage["mean_total_tokens"] = (
        model_usage["total_tokens"] / valid_count if valid_count else 0.0
    )
    model_usage["mean_estimated_cost_usd"] = (
        model_usage["estimated_cost_usd"] / valid_count if valid_count else 0.0
    )
    timing = {
        key: _numeric_summary(float(item.get(key) or 0.0) for item in timing_rows)
        for key in ("agent_execution_seconds", "end_to_end_seconds")
    }

    field_minimum = min(
        (values["minimum"] for values in field_accuracy.values()),
        default=0.0,
    )
    configured = dict(readiness_config or {})
    readiness = {
        "required_valid_runs": max(1, int(required_valid_runs)),
        "mean_family_coverage": float(configured.get("mean_family_coverage", 0.98)),
        "minimum_family_coverage": float(
            configured.get("minimum_family_coverage", 0.96)
        ),
        "minimum_recovery_recall": float(
            configured.get("minimum_recovery_recall", 0.98)
        ),
        "minimum_parsing_precision": float(
            configured.get("minimum_parsing_precision", 0.98)
        ),
        "minimum_field_accuracy": float(
            configured.get("minimum_field_accuracy", 0.98)
        ),
        "minimum_pooled_behavior_coverage": float(
            configured.get("minimum_pooled_behavior_coverage", 0.0)
        ),
        "maximum_actions_after_last_new_target": int(
            configured.get("maximum_actions_after_last_new_target", 1_000_000)
        ),
        "maximum_duplicate_navigation_count": int(
            configured.get("maximum_duplicate_navigation_count", 0)
        ),
        "maximum_identical_executed_signature_streak": int(
            configured.get(
                "maximum_identical_executed_signature_streak",
                configured.get("maximum_identical_signature_streak", 2),
            )
        ),
        "baseline_mean_total_tokens": float(
            configured.get("baseline_mean_total_tokens", 0.0)
        ),
        "baseline_mean_estimated_cost_usd": float(
            configured.get("baseline_mean_estimated_cost_usd", 0.0)
        ),
        "minimum_efficiency_reduction": float(
            configured.get("minimum_efficiency_reduction", 0.0)
        ),
    }
    token_baseline = readiness["baseline_mean_total_tokens"]
    cost_baseline = readiness["baseline_mean_estimated_cost_usd"]
    model_usage["token_reduction_from_baseline"] = (
        1.0 - model_usage["mean_total_tokens"] / token_baseline
        if token_baseline > 0
        else None
    )
    model_usage["cost_reduction_from_baseline"] = (
        1.0 - model_usage["mean_estimated_cost_usd"] / cost_baseline
        if cost_baseline > 0
        else None
    )
    agent_summaries = [
        item.get("agent_summary")
        if isinstance(item.get("agent_summary"), Mapping)
        else {}
        for item in valid
    ]
    readiness_checks = {
        "run_count": (
            len(attempts) == readiness["required_valid_runs"]
            and len(valid) == readiness["required_valid_runs"]
        ),
        "mean_family_coverage": coverage["mean"] >= readiness["mean_family_coverage"],
        "minimum_family_coverage": coverage["minimum"]
        >= readiness["minimum_family_coverage"],
        "recovery_recall": recovery["minimum"]
        >= readiness["minimum_recovery_recall"],
        "parsing_precision": precision["minimum"]
        >= readiness["minimum_parsing_precision"],
        "field_accuracy": field_minimum >= readiness["minimum_field_accuracy"],
        "pooled_behavior_coverage": (
            behavior["pooled"] >= readiness["minimum_pooled_behavior_coverage"]
            if readiness["minimum_pooled_behavior_coverage"] > 0
            else True
        ),
        "autonomous_completion": all(
            summary.get("autonomous_completion") is True
            for summary in agent_summaries
        ),
        "no_max_action_stop": all(
            str(summary.get("stop_reason") or "") != "max_actions_total"
            for summary in agent_summaries
        ),
        "post_target_action_tail": actions["actions_after_last_new_target"][
            "maximum"
        ]
        <= readiness["maximum_actions_after_last_new_target"],
        "duplicate_navigation": actions["duplicate_navigation_count"]
        <= readiness["maximum_duplicate_navigation_count"],
        "identical_executed_signature_streak": actions[
            "longest_identical_executed_signature_streak"
        ]
        <= readiness["maximum_identical_executed_signature_streak"],
        "token_efficiency": (
            model_usage["token_reduction_from_baseline"]
            >= readiness["minimum_efficiency_reduction"]
            if token_baseline > 0
            else True
        ),
        "cost_efficiency": (
            model_usage["cost_reduction_from_baseline"]
            >= readiness["minimum_efficiency_reduction"]
            if cost_baseline > 0
            else True
        ),
    }
    ready = bool(readiness_checks) and all(readiness_checks.values())
    return {
        "schema_version": "1.0.0",
        "campaign_id": campaign_id,
        "attempted_run_count": len(attempts),
        "valid_run_count": len(valid),
        "valid_run_ids": [str(item.get("run_id") or "") for item in valid],
        "invalid_runs": invalid_runs,
        "family_coverage": coverage,
        "recovery_recall": recovery,
        "parsing_precision": precision,
        "behavior_coverage": behavior,
        "field_accuracy": field_accuracy,
        "actions": actions,
        "model_usage": model_usage,
        "timing": timing,
        "readiness_thresholds": readiness,
        "readiness_checks": readiness_checks,
        "ready_for_freeze_review": ready,
    }


def _run_autonomous_cli(args: argparse.Namespace, local: Mapping[str, Any]) -> int:
    results_root = Path(str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS))
    results_root.mkdir(parents=True, exist_ok=True)
    foundation = validate_research_archive_foundation()
    if not foundation["ok"]:
        print("[FAIL] Research Archive foundation validation failed.", file=sys.stderr)
        return 2

    report = preflight(
        local,
        dry_run=False,
        require_guest_deploy=args.mode != "autonomous-preflight",
    )
    if not report["ok"]:
        for item in report["checks"]:
            if not item["ok"]:
                print(f"[FAIL] {item['name']}: {item['detail']}", file=sys.stderr)
        return 2
    if args.mode == "autonomous-preflight":
        for item in report["checks"]:
            print(f"[{'PASS' if item['ok'] else 'FAIL'}] {item['name']}: {item['detail']}")
        return 0

    web_url_base = str(os.getenv("WEB_URL") or "").strip()
    if not web_url_base:
        print("[FAIL] WEB_URL is required for this web-based evaluation.", file=sys.stderr)
        return 2
    try:
        parsed_web = urlsplit(build_archive_start_url(web_url_base, "preflight"))
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    site = local.get("site") or {}
    configured_port = int(site.get("port") or 8765)
    web_port = parsed_web.port or (80 if parsed_web.scheme.lower() == "http" else 443)
    if web_port != configured_port:
        print(
            f"[FAIL] WEB_URL port {web_port} does not match site.port {configured_port}.",
            file=sys.stderr,
        )
        return 2

    smoke_gate = results_root / "autonomous_smoke_gate.json"
    freeze_gate = results_root / "autonomous_freeze_manifest.json"
    if args.mode == "autonomous-pilot":
        gate_ok, gate_reason = autonomous_smoke_gate_status(smoke_gate)
        if not gate_ok:
            print(
                f"[FAIL] Run autonomous-smoke successfully before a Pilot: "
                f"{gate_reason}.",
                file=sys.stderr,
            )
            return 3
    if args.mode == "autonomous-campaign":
        gate_ok, gate_reason = autonomous_freeze_gate_status(
            freeze_gate,
            expected_fingerprint=autonomous_framework_fingerprint(),
        )
        if not gate_ok:
            print(
                "[FAIL] Campaign remains locked until a separately reviewed and "
                f"user-approved freeze manifest is valid: {gate_reason}.",
                file=sys.stderr,
            )
            return 3

    count = resolve_autonomous_run_count(args.mode, args.count)
    if args.mode == "autonomous-smoke":
        campaign_id = "smoke_" + _now_id()
    elif args.mode == "autonomous-pilot":
        campaign_id = "pilot_" + _now_id()
    else:
        campaign_id = "campaign_" + _now_id()

    connection: Optional[rpyc.Connection] = None
    archive_runtime: Optional[ResearchArchiveProcess] = None
    failure: Optional[Exception] = None
    current_run_id = ""
    current_run_dir: Optional[Path] = None
    outcome = 0
    try:
        print("[STAGE] Starting A1 VM", flush=True)
        ensure_vm_started(local, os.environ)
        deployment = provision_vm_agent(local, os.environ)
        guest_ip = str(deployment.get("guest_ip") or "")
        guest_port = int(deployment.get("port") or (local.get("vm") or {}).get("rpyc_port") or 18861)
        if not guest_ip:
            raise RuntimeError("VM deployment did not return the guest IP")
        connection = connect_vm_agent(
            guest_ip,
            guest_port,
            timeout_sec=int((local.get("vm") or {}).get("wait_timeout_sec") or 240),
        )

        print("[STAGE] Opening the visible Research Archive server console", flush=True)
        archive_runtime = ResearchArchiveProcess(
            host=str(site.get("bind_host") or "0.0.0.0"),
            port=configured_port,
            control_port=int(site.get("control_port") or (configured_port + 1)),
            log_dir=results_root,
            root=ROOT,
        )
        archive_client = archive_runtime.start()

        last_result: Optional[Dict[str, Any]] = None
        attempt_results: list[Dict[str, Any]] = []
        for index in range(count):
            run_id = (
                args.run_id
                if count == 1 and args.run_id
                else f"{campaign_id}_{index + 1:02d}"
            )
            current_run_id = run_id
            current_run_dir = results_root / f"run_{run_id}"
            print(f"[RUN] {run_id} ({index + 1}/{count})", flush=True)
            last_result = run_autonomous_once(
                local,
                campaign_id,
                run_id,
                archive_client=archive_client,
                vm_connection_info=deployment,
                web_url_base=web_url_base,
            )
            attempt_results.append(last_result)
            print(json.dumps(last_result["comparison"], ensure_ascii=False), flush=True)
            if args.mode == "autonomous-pilot":
                attempt_ok, attempt_reasons = autonomous_smoke_gate_passes(last_result)
                if not attempt_ok:
                    print(
                        f"[FAIL] Pilot stopped after invalid run {run_id}: "
                        f"{', '.join(attempt_reasons)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    outcome = 3
                    break

        if args.mode == "autonomous-smoke" and last_result is not None:
            gate_passed, gate_reasons = autonomous_smoke_gate_passes(last_result)
            gate_record = {
                "schema_version": "1.0.0",
                "run_id": last_result["run_id"],
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "foundation": foundation,
                "comparison": last_result["comparison"],
                "agent_summary": last_result.get("agent_summary") or {},
                "framework_fingerprint": autonomous_framework_fingerprint(),
                "passed": gate_passed,
                "reasons": gate_reasons,
            }
            _write_json(smoke_gate, gate_record)
            if gate_passed:
                print(f"[PASS] Autonomous smoke gate written: {smoke_gate}", flush=True)
            else:
                failure_path = results_root / f"autonomous_smoke_failure_{last_result['run_id']}.json"
                _write_json(failure_path, gate_record)
                print(
                    f"[FAIL] Autonomous smoke gate rejected: {', '.join(gate_reasons)}; "
                    f"details={failure_path}",
                    file=sys.stderr,
                    flush=True,
                )
                outcome = 3
        elif args.mode == "autonomous-pilot":
            pilot_summary = summarize_autonomous_pilot(
                campaign_id,
                attempt_results,
                required_valid_runs=count,
                readiness_config=(local.get("readiness") or {}),
            )
            summary_path = results_root / f"autonomous_pilot_summary_{campaign_id}.json"
            _write_json(summary_path, pilot_summary)
            if pilot_summary["ready_for_freeze_review"]:
                print(
                    f"[PASS] {count}-run Pilot is ready for freeze review: {summary_path}",
                    flush=True,
                )
            else:
                print(
                    f"[FAIL] {count}-run Pilot did not meet the freeze-review thresholds: "
                    f"{summary_path}",
                    file=sys.stderr,
                    flush=True,
                )
                outcome = 3
    except Exception as exc:
        failure = exc
        outcome = 4
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if current_run_dir is not None:
            failure_record = {
                "schema_version": "1.0.0",
                "run_id": current_run_id,
                "campaign_id": campaign_id,
                "mode": args.mode,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                **classify_autonomous_attempt_failure(current_run_dir, exc),
            }
            _write_json(current_run_dir / "attempt_failure.json", failure_record)
            print(
                f"[INFO] Structured attempt failure written: "
                f"{current_run_dir / 'attempt_failure.json'}",
                flush=True,
            )
        hold_seconds = max(0, int(args.keep_server_on_failure_seconds))
        if hold_seconds:
            print(f"[INFO] Keeping the server console visible for {hold_seconds} seconds.", flush=True)
            time.sleep(hold_seconds)
    finally:
        if archive_runtime is not None:
            archive_runtime.stop()
        if connection is not None:
            connection.close()
    return outcome if failure is not None else outcome


def _resolve_source_run(local: Mapping[str, Any], value: Optional[str]) -> Path:
    if not value:
        raise ValueError("--source-run is required")
    supplied = Path(value)
    if supplied.is_absolute():
        return supplied
    results_root = Path(str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS))
    direct = results_root / supplied
    if direct.exists():
        return direct
    prefixed = results_root / f"run_{value}"
    return prefixed if prefixed.exists() else direct


def _source_pipeline_timing(source_run_dir: Path) -> Dict[str, Any]:
    source_run_dir = Path(source_run_dir)
    run_manifest = _read_json(source_run_dir / "run_manifest.json")
    snapshot_manifest = _read_json(source_run_dir / "snapshot_manifest.json")
    snapshot_id = str(snapshot_manifest.get("snapshot_id") or "").strip()
    started_at = str((run_manifest.get("timing") or {}).get("end_to_end_started_at") or "")
    if not snapshot_id or not started_at:
        raise ValueError(f"Source run is missing pipeline timing evidence: {source_run_dir}")
    candidates = [
        path
        for path in (source_run_dir / "przma").rglob("engine_record.json")
        if path.parent.name == snapshot_id
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one engine_record.json for selected snapshot {snapshot_id}; "
            f"found {len(candidates)}"
        )
    engine_record = _read_json(candidates[0])
    persisted_at = str(engine_record.get("created_at") or "")
    seconds = (
        datetime.fromisoformat(persisted_at) - datetime.fromisoformat(started_at)
    ).total_seconds()
    return {
        "snapshot_id": snapshot_id,
        "started_at": started_at,
        "snapshot_persisted_at": persisted_at,
        "seconds": seconds,
        "engine_record": str(candidates[0].relative_to(source_run_dir)),
    }


def _paired_replay_summary_row(
    pair_index: int,
    manifest: Mapping[str, Any],
    pipeline_seconds: float,
) -> Dict[str, Any]:
    return {
        "pair_index": pair_index,
        "source_run_id": manifest["source_run_id"],
        "replay_run_id": manifest["run_id"],
        "matching_actions": manifest["matching_action_count"],
        "replay_actions": manifest["replay_action_count"],
        "source_actions": manifest["source_action_count"],
        "replay_action_agreement": manifest["replay_action_agreement"],
        "reproduced_families": manifest["reproduced_family_count"],
        "source_generated_families": manifest["source_generated_family_count"],
        "replay_family_reproduction": manifest["replay_family_reproduction"],
        "end_to_end_pipeline_seconds": pipeline_seconds,
    }


def _run_paired_replay_cli(args: argparse.Namespace, local: Mapping[str, Any]) -> int:
    selection_path = (
        Path(args.selection)
        if args.selection
        else ROOT / "evaluation" / "results" / "aggregate" / "final20_selection.json"
    )
    if not selection_path.is_absolute():
        selection_path = ROOT / selection_path
    selection = _read_json(selection_path)
    source_run_ids = [str(value) for value in selection.get("included_run_ids") or []]
    if len(source_run_ids) != 20 or len(set(source_run_ids)) != 20:
        raise ValueError("Paired Replay requires exactly 20 unique included_run_ids")

    foundation = validate_research_archive_foundation()
    if not foundation["ok"]:
        print("[FAIL] Research Archive foundation validation failed.", file=sys.stderr)
        return 2
    report = preflight(local, dry_run=False, require_guest_deploy=True)
    if not report["ok"]:
        for item in report["checks"]:
            if not item["ok"]:
                print(f"[FAIL] {item['name']}: {item['detail']}", file=sys.stderr)
        return 2

    web_url_base = str(os.getenv("WEB_URL") or "").strip()
    if not web_url_base:
        raise ValueError("WEB_URL is required for Paired Replay")
    campaign_id = str(args.run_id or f"paired_replay_{_now_id()}")
    site = local.get("site") or {}
    configured_port = int(site.get("port") or 8765)
    parsed_web = urlsplit(build_archive_start_url(web_url_base, campaign_id))
    web_port = parsed_web.port or (80 if parsed_web.scheme.lower() == "http" else 443)
    if web_port != configured_port:
        raise ValueError(f"WEB_URL port {web_port} does not match site.port {configured_port}")

    results_root = Path(str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS))
    aggregate_dir = results_root / "aggregate"
    manifests: list[Dict[str, Any]] = []
    connection: Optional[rpyc.Connection] = None
    archive_runtime: Optional[ResearchArchiveProcess] = None
    try:
        print("[STAGE] Starting A1 VM for 20 paired Action Replays", flush=True)
        ensure_vm_started(local, os.environ)
        deployment = provision_vm_agent(local, os.environ)
        guest_ip = str(deployment.get("guest_ip") or "")
        guest_port = int(
            deployment.get("port")
            or (local.get("vm") or {}).get("rpyc_port")
            or 18861
        )
        if not guest_ip:
            raise RuntimeError("VM deployment did not return the guest IP")
        connection = connect_vm_agent(
            guest_ip,
            guest_port,
            timeout_sec=int((local.get("vm") or {}).get("wait_timeout_sec") or 240),
        )
        print("[STAGE] Opening the visible Research Archive server console", flush=True)
        archive_runtime = ResearchArchiveProcess(
            host=str(site.get("bind_host") or "0.0.0.0"),
            port=configured_port,
            control_port=int(site.get("control_port") or (configured_port + 1)),
            log_dir=results_root,
            root=ROOT,
        )
        archive_client = archive_runtime.start()
        for index, source_run_id in enumerate(source_run_ids, start=1):
            source_run = _resolve_source_run(local, source_run_id)
            if not source_run.is_dir():
                raise FileNotFoundError(f"Source run does not exist: {source_run}")
            replay_run_id = f"{campaign_id}_{index:02d}"
            print(
                f"[REPLAY] {index}/20 source={source_run_id} replay={replay_run_id}",
                flush=True,
            )
            manifests.append(
                run_action_replay_once(
                    local,
                    source_run,
                    replay_run_id,
                    archive_client=archive_client,
                    connection=connection,
                    web_url_base=web_url_base,
                )
            )
    finally:
        if archive_runtime is not None:
            archive_runtime.stop()
        if connection is not None:
            connection.close()

    source_pipeline_timing = [
        {
            "source_run_id": source_run_id,
            **_source_pipeline_timing(_resolve_source_run(local, source_run_id)),
        }
        for source_run_id in source_run_ids
    ]
    aggregate = aggregate_paired_action_replays(
        manifests,
        pipeline_seconds=[float(item["seconds"]) for item in source_pipeline_timing],
    )
    aggregate.update(
        {
            "campaign_id": campaign_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "selection_manifest": str(selection_path.resolve()),
            "selection_manifest_sha256": _sha256_file(selection_path),
            "model_calls": 0,
            "source_pipeline_timing": source_pipeline_timing,
        }
    )
    inventory = []
    for manifest in manifests:
        replay_dir = results_root / f"run_{manifest['run_id']}"
        for name in (
            "replay_manifest.json",
            "replay_actions.jsonl",
            "archive_requests.jsonl",
            "site_status.json",
        ):
            path = replay_dir / name
            if path.is_file():
                inventory.append(
                    {
                        "run_id": manifest["run_id"],
                        "path": str(path.relative_to(ROOT)),
                        "size": path.stat().st_size,
                        "sha256": _sha256_file(path),
                    }
                )
    aggregate["artifact_inventory"] = inventory
    aggregate_path = aggregate_dir / "final20_paired_replay_metrics.json"
    _write_json(aggregate_path, aggregate)
    csv_path = aggregate_dir / "final20_paired_replay_summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "pair_index",
            "source_run_id",
            "replay_run_id",
            "matching_actions",
            "replay_actions",
            "source_actions",
            "replay_action_agreement",
            "reproduced_families",
            "source_generated_families",
            "replay_family_reproduction",
            "end_to_end_pipeline_seconds",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, manifest in enumerate(manifests, start=1):
            writer.writerow(
                _paired_replay_summary_row(
                    index,
                    manifest,
                    float(source_pipeline_timing[index - 1]["seconds"]),
                )
            )
    print(json.dumps(aggregate, ensure_ascii=False), flush=True)
    return 0


def _run_replay_cli(args: argparse.Namespace, local: Mapping[str, Any]) -> int:
    try:
        source_run = _resolve_source_run(local, args.source_run)
        if not source_run.is_dir():
            raise FileNotFoundError(f"Source run does not exist: {source_run}")

        if args.mode == "recovery-replay":
            results_root = Path(
                str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS)
            )
            output_dir = (
                Path(args.output)
                if args.output
                else results_root / f"recovery_replay_{_now_id()}"
            )
            tool = local.get("tool") or {}
            result = recovery_replay(
                source_run,
                output_dir,
                tool_exe=Path(str(tool.get("exe_path") or "")),
                resource_manifest_path=(
                    ROOT
                    / "evaluation"
                    / "cache_recovery"
                    / "research_archive"
                    / "resource_manifest.json"
                ),
                behavior_manifest_path=(
                    ROOT
                    / "evaluation"
                    / "cache_recovery"
                    / "research_archive"
                    / "behavior_manifest.json"
                ),
                size_semantics=tool.get("size_semantics") or None,
                content_semantics=tool.get("content_semantics") or None,
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0

        foundation = validate_research_archive_foundation()
        if not foundation["ok"]:
            print("[FAIL] Research Archive foundation validation failed.", file=sys.stderr)
            return 2
        report = preflight(local, dry_run=False, require_guest_deploy=True)
        if not report["ok"]:
            for item in report["checks"]:
                if not item["ok"]:
                    print(f"[FAIL] {item['name']}: {item['detail']}", file=sys.stderr)
            return 2

        web_url_base = str(os.getenv("WEB_URL") or "").strip()
        if not web_url_base:
            raise ValueError("WEB_URL is required for Action Replay")
        replay_run_id = str(args.run_id or f"action_replay_{_now_id()}")
        site = local.get("site") or {}
        configured_port = int(site.get("port") or 8765)
        parsed_web = urlsplit(build_archive_start_url(web_url_base, replay_run_id))
        web_port = parsed_web.port or (80 if parsed_web.scheme.lower() == "http" else 443)
        if web_port != configured_port:
            raise ValueError(
                f"WEB_URL port {web_port} does not match site.port {configured_port}"
            )

        connection: Optional[rpyc.Connection] = None
        archive_runtime: Optional[ResearchArchiveProcess] = None
        try:
            print("[STAGE] Starting A1 VM for Action Replay", flush=True)
            ensure_vm_started(local, os.environ)
            deployment = provision_vm_agent(local, os.environ)
            guest_ip = str(deployment.get("guest_ip") or "")
            guest_port = int(
                deployment.get("port")
                or (local.get("vm") or {}).get("rpyc_port")
                or 18861
            )
            if not guest_ip:
                raise RuntimeError("VM deployment did not return the guest IP")
            connection = connect_vm_agent(
                guest_ip,
                guest_port,
                timeout_sec=int((local.get("vm") or {}).get("wait_timeout_sec") or 240),
            )
            print(
                "[STAGE] Opening the visible Research Archive server console for Action Replay",
                flush=True,
            )
            results_root = Path(
                str((local.get("paths") or {}).get("results_root") or DEFAULT_RESULTS)
            )
            archive_runtime = ResearchArchiveProcess(
                host=str(site.get("bind_host") or "0.0.0.0"),
                port=configured_port,
                control_port=int(site.get("control_port") or (configured_port + 1)),
                log_dir=results_root,
                root=ROOT,
            )
            archive_client = archive_runtime.start()
            result = run_action_replay_once(
                local,
                source_run,
                replay_run_id,
                archive_client=archive_client,
                connection=connection,
                web_url_base=web_url_base,
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        finally:
            if archive_runtime is not None:
                archive_runtime.stop()
            if connection is not None:
                connection.close()
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 4


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PrZMA cache-recovery evaluation")
    parser.add_argument(
        "mode",
        choices=(
            "autonomous-preflight",
            "autonomous-smoke",
            "autonomous-pilot",
            "autonomous-campaign",
            "action-replay",
            "paired-action-replay",
            "recovery-replay",
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--count", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--seed")
    parser.add_argument("--source-run")
    parser.add_argument("--selection")
    parser.add_argument("--output")
    parser.add_argument("--keep-server-on-failure-seconds", type=int, default=15)
    args = parser.parse_args(list(argv) if argv is not None else None)
    local = _load_local_config(args.config)

    if args.mode in {"action-replay", "recovery-replay"}:
        return _run_replay_cli(args, local)

    if args.mode == "paired-action-replay":
        try:
            return _run_paired_replay_cli(args, local)
        except Exception as exc:
            print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 4

    if args.mode.startswith("autonomous-"):
        return _run_autonomous_cli(args, local)

    parser.error(f"unsupported evaluation mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
