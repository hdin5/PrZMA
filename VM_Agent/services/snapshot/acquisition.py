from __future__ import annotations

import errno
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


ACQUISITION_STATUSES = {
    "collected",
    "partially_collected",
    "not_present",
    "permission_denied",
    "locked",
    "limit_exceeded",
    "unsupported",
    "acquisition_failed",
}


@dataclass
class AcquisitionRequest:
    artifact_key: str
    layer: str
    source: str
    source_type: str = "file"
    strategy: List[str] = field(default_factory=lambda: ["direct"])
    availability: str = "required"
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "AcquisitionRequest":
        strategy = value.get("strategy") or ["direct"]
        if not isinstance(strategy, list):
            strategy = [str(strategy)]
        meta = value.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        return cls(
            artifact_key=str(value.get("artifact_key") or "unnamed"),
            layer=str(value.get("layer") or "browser_artifacts"),
            source=str(value.get("source") or ""),
            source_type=str(value.get("source_type") or "file"),
            strategy=[str(item) for item in strategy],
            availability=str(value.get("availability") or "required"),
            meta=dict(meta),
        )


@dataclass
class AcquisitionOutcome:
    artifact_key: str
    layer: str
    source: str
    status: str
    availability: str = "required"
    attempted_methods: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    byte_count: int = 0
    error_code: Optional[str] = None
    message: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        if self.status not in ACQUISITION_STATUSES:
            raise ValueError(f"Unsupported acquisition status: {self.status}")
        return asdict(self)


def validate_config_source(source: str) -> Optional[str]:
    if not source or "\x00" in source:
        return "empty_or_nul_source"
    normalized = source.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    if ".." in parts:
        return "parent_traversal"
    if normalized.startswith("\\\\.\\") or normalized.startswith("\\\\?\\"):
        return "device_path"
    if normalized.startswith("\\\\"):
        return "unc_path"
    return None


def classify_os_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, FileNotFoundError):
        return "not_present", "not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied", "access_denied"
    if isinstance(exc, TimeoutError):
        return "acquisition_failed", "timeout"
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror in {32, 33}:
            return "locked", f"winerror_{winerror}"
        if winerror == 5:
            return "permission_denied", "winerror_5"
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return "permission_denied", f"errno_{exc.errno}"
        if exc.errno == errno.ENOENT:
            return "not_present", f"errno_{exc.errno}"
        return "acquisition_failed", (
            f"winerror_{winerror}" if winerror is not None else f"errno_{exc.errno}"
        )
    return "acquisition_failed", type(exc).__name__


def legacy_request(layer: str, encoded_path: str) -> AcquisitionRequest:
    artifact_key = "unnamed"
    source = encoded_path
    if "||" in encoded_path:
        raw_key, raw_source = encoded_path.split("||", 1)
        artifact_key = raw_key.strip() or "unnamed"
        source = raw_source.strip()
    return AcquisitionRequest(
        artifact_key=artifact_key,
        layer=layer,
        source=source,
        source_type="file",
        strategy=["direct"],
        availability="required",
    )


def canonical_source_key(path: Path) -> str:
    value = os.path.normcase(os.path.abspath(str(path)))
    return value.rstrip("\\/")
