from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List

from .windows_collectors import CommandResult, CommandRunner, run_bounded_command


_VOLUME_RE = re.compile(r"^[A-Za-z]:\\?$")
_SHADOW_ID_RE = re.compile(
    r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)


class VssSnapshotError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _normalize_volume(volume: str) -> str:
    value = str(volume).strip()
    if not _VOLUME_RE.fullmatch(value):
        raise VssSnapshotError("invalid_volume", f"Unsupported VSS volume: {value}")
    return value[0].upper() + ":\\"


def _command_error(result: CommandResult) -> VssSnapshotError:
    if result.timed_out:
        return VssSnapshotError("timeout", result.stderr or "VSS command timed out.")
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "access is denied" in text or "access denied" in text:
        return VssSnapshotError("access_denied", result.stderr or result.stdout)
    if "not supported" in text or "invalid class" in text:
        return VssSnapshotError("unsupported", result.stderr or result.stdout)
    return VssSnapshotError(
        f"exit_{result.returncode}",
        result.stderr or result.stdout or "VSS command failed.",
    )


@dataclass
class VssShadow:
    shadow_id: str
    volume: str
    device_object: str

    def map_path(self, source_path: str) -> str:
        normalized = str(source_path).replace("/", "\\")
        source_volume = _normalize_volume(normalized[:3])
        if source_volume != self.volume:
            raise VssSnapshotError(
                "volume_mismatch",
                f"{source_path} does not belong to {self.volume}",
            )
        relative = normalized[3:].lstrip("\\")
        return self.device_object.rstrip("\\") + "\\" + relative


class VssSnapshotProvider:
    def __init__(
        self,
        runner: CommandRunner = run_bounded_command,
        *,
        timeout: int = 120,
    ):
        self.runner = runner
        self.timeout = timeout
        self._shadows: Dict[str, VssShadow] = {}
        self._closed = False

    def __enter__(self) -> "VssSnapshotProvider":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close_all()

    def open_volume(self, volume: str) -> VssShadow:
        normalized_volume = _normalize_volume(volume)
        existing = self._shadows.get(normalized_volume)
        if existing is not None:
            return existing
        if self._closed:
            raise VssSnapshotError("provider_closed", "VSS provider is already closed.")

        script = (
            "$ErrorActionPreference='Stop';"
            "$class=[WMIClass]'root\\cimv2:Win32_ShadowCopy';"
            f"$result=$class.Create('{normalized_volume}','ClientAccessible');"
            "if($result.ReturnValue -ne 0){"
            "throw ('VSS Create failed with code '+$result.ReturnValue)};"
            "$shadow=Get-CimInstance -ClassName Win32_ShadowCopy | "
            "Where-Object {$_.ID -eq $result.ShadowID};"
            "if($null -eq $shadow){throw 'Created shadow was not found'};"
            "[pscustomobject]@{"
            "ShadowID=$shadow.ID;"
            "DeviceObject=$shadow.DeviceObject;"
            f"Volume='{normalized_volume}'"
            "}|ConvertTo-Json -Compress"
        )
        result = self.runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            self.timeout,
        )
        if result.returncode != 0:
            raise _command_error(result)

        try:
            payload = json.loads(result.stdout.strip())
            shadow_id = str(payload["ShadowID"])
            device_object = str(payload["DeviceObject"])
        except Exception as exc:
            raise VssSnapshotError(
                "invalid_response",
                f"VSS create returned invalid JSON: {exc}",
            ) from exc
        if not _SHADOW_ID_RE.fullmatch(shadow_id):
            raise VssSnapshotError("invalid_shadow_id", "VSS returned an invalid shadow ID.")
        if not device_object.startswith("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy"):
            raise VssSnapshotError(
                "invalid_device_object",
                "VSS returned an unexpected device object.",
            )

        shadow = VssShadow(
            shadow_id=shadow_id,
            volume=normalized_volume,
            device_object=device_object,
        )
        self._shadows[normalized_volume] = shadow
        return shadow

    def close_all(self) -> List[Dict[str, object]]:
        if self._closed:
            return []
        self._closed = True
        results: List[Dict[str, object]] = []
        for shadow in list(self._shadows.values()):
            result = self.runner(
                [
                    "vssadmin.exe",
                    "Delete",
                    "Shadows",
                    f"/Shadow={shadow.shadow_id}",
                    "/Quiet",
                ],
                self.timeout,
            )
            results.append(
                {
                    "shadow_id": shadow.shadow_id,
                    "volume": shadow.volume,
                    "removed": result.returncode == 0,
                    "error": None if result.returncode == 0 else (result.stderr or result.stdout),
                }
            )
        self._shadows.clear()
        return results
