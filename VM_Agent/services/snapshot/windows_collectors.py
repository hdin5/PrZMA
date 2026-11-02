from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class NativeCollectionResult:
    status: str
    files: List[Path] = field(default_factory=list)
    failures: List[Dict[str, object]] = field(default_factory=list)


CommandRunner = Callable[[List[str], int], CommandResult]


@dataclass
class UserContext:
    user_name: Optional[str] = None
    user_sid: Optional[str] = None
    user_profile: Optional[str] = None
    source: str = "active_session"
    error_code: Optional[str] = None
    message: Optional[str] = None

    @property
    def appdata(self) -> Optional[str]:
        if not self.user_profile:
            return None
        return str(Path(self.user_profile) / "AppData" / "Roaming")

    @property
    def localappdata(self) -> Optional[str]:
        if not self.user_profile:
            return None
        return str(Path(self.user_profile) / "AppData" / "Local")

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "user_name": self.user_name,
            "user_sid": self.user_sid,
            "user_profile": self.user_profile,
            "appdata": self.appdata,
            "localappdata": self.localappdata,
            "source": self.source,
            "error_code": self.error_code,
            "message": self.message,
        }


def run_bounded_command(args: List[str], timeout: int) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return CommandResult(
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(
            returncode=-1,
            stdout=stdout,
            stderr=stderr or f"command exceeded {timeout} seconds",
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=str(exc),
        )


class UserContextResolver:
    """Resolve the active interactive Windows user independently of the service account."""

    _SCRIPT = (
        "$ErrorActionPreference='Stop';"
        "$name=(Get-CimInstance Win32_ComputerSystem).UserName;"
        "if([string]::IsNullOrWhiteSpace($name)){"
        "Write-Error 'No active interactive user session.';exit 4};"
        "$sid=(New-Object System.Security.Principal.NTAccount($name))."
        "Translate([System.Security.Principal.SecurityIdentifier]).Value;"
        "$key='Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\'+$sid;"
        "$profile=(Get-ItemProperty -LiteralPath $key -Name ProfileImagePath).ProfileImagePath;"
        "[pscustomobject]@{user_name=$name;user_sid=$sid;user_profile=[Environment]::ExpandEnvironmentVariables($profile)}"
        "|ConvertTo-Json -Compress"
    )

    def __init__(self, runner: CommandRunner = run_bounded_command, timeout: int = 30):
        self.runner = runner
        self.timeout = timeout

    def resolve(self) -> UserContext:
        result = self.runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                self._SCRIPT,
            ],
            self.timeout,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Active user lookup failed.").strip()
            code = (
                "active_user_unavailable"
                if result.returncode == 4 or "no active interactive user" in message.lower()
                else _failure_code(result)
            )
            return UserContext(error_code=code, message=message)
        try:
            payload = json.loads(result.stdout.strip())
            return UserContext(
                user_name=str(payload.get("user_name") or "") or None,
                user_sid=str(payload.get("user_sid") or "") or None,
                user_profile=str(payload.get("user_profile") or "") or None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return UserContext(
                error_code="invalid_user_context",
                message=f"Active user lookup returned invalid JSON: {exc}",
            )


def _failure_code(result: CommandResult) -> str:
    if result.timed_out:
        return "timeout"
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "access is denied" in text or "access denied" in text:
        return "access_denied"
    if "cannot find" in text or "not found" in text:
        return "not_found"
    return f"exit_{result.returncode}"


def _aggregate_status(files: List[Path], failures: List[Dict[str, object]]) -> str:
    if files and failures:
        return "partially_collected"
    if files:
        return "collected"
    return "acquisition_failed"


def _registry_output_name(source: str) -> str:
    upper = source.upper()
    if upper.endswith("_CLASSES"):
        return "UsrClass.dat"
    if upper.startswith("HKU\\"):
        return "NTUSER.DAT"
    return source.rsplit("\\", 1)[-1]


class RegistryCollector:
    _SOURCE_RE = re.compile(r"^(?:HKLM|HKU)\\[A-Za-z0-9_.{}-]+$", re.IGNORECASE)

    def __init__(self, runner: CommandRunner = run_bounded_command, timeout: int = 60):
        self.runner = runner
        self.timeout = timeout

    def collect(
        self,
        *,
        native_sources: List[str],
        destination: Path,
        user_sid: Optional[str] = None,
    ) -> NativeCollectionResult:
        destination.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        failures: List[Dict[str, object]] = []

        for raw_source in native_sources:
            source = str(raw_source)
            if "{USER_SID}" in source:
                if not user_sid:
                    failures.append(
                        {
                            "source": source,
                            "error_code": "user_sid_unavailable",
                            "message": "No user SID was available for the selected profile.",
                        }
                    )
                    continue
                source = source.replace("{USER_SID}", user_sid)

            if not self._SOURCE_RE.fullmatch(source):
                failures.append(
                    {
                        "source": source,
                        "error_code": "invalid_registry_source",
                        "message": "Registry source is not permitted by the collector.",
                    }
                )
                continue

            output = destination / _registry_output_name(source)
            result = self.runner(
                ["reg.exe", "save", source, str(output), "/y"],
                self.timeout,
            )
            if result.returncode == 0 and output.is_file():
                files.append(output)
                continue

            failures.append(
                {
                    "source": source,
                    "error_code": _failure_code(result),
                    "message": (result.stderr or result.stdout or "Registry export failed.").strip(),
                    "returncode": result.returncode,
                }
            )

        return NativeCollectionResult(
            status=_aggregate_status(files, failures),
            files=files,
            failures=failures,
        )


class EventLogCollector:
    _CHANNEL_RE = re.compile(r"^[A-Za-z0-9_. /-]+$")

    def __init__(self, runner: CommandRunner = run_bounded_command, timeout: int = 60):
        self.runner = runner
        self.timeout = timeout

    def collect(
        self,
        *,
        channels: List[str],
        destination: Path,
    ) -> NativeCollectionResult:
        destination.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        failures: List[Dict[str, object]] = []

        for raw_channel in channels:
            channel = str(raw_channel)
            if not self._CHANNEL_RE.fullmatch(channel) or ".." in channel:
                failures.append(
                    {
                        "source": channel,
                        "error_code": "invalid_event_channel",
                        "message": "Event Log channel is not permitted by the collector.",
                    }
                )
                continue

            safe_name = channel.replace("/", "_").replace("\\", "_")
            output = destination / f"{safe_name}.evtx"
            result = self.runner(
                ["wevtutil.exe", "epl", channel, str(output), "/ow:true"],
                self.timeout,
            )
            if result.returncode == 0 and output.is_file():
                files.append(output)
                continue

            failures.append(
                {
                    "source": channel,
                    "error_code": _failure_code(result),
                    "message": (result.stderr or result.stdout or "Event Log export failed.").strip(),
                    "returncode": result.returncode,
                }
            )

        return NativeCollectionResult(
            status=_aggregate_status(files, failures),
            files=files,
            failures=failures,
        )
