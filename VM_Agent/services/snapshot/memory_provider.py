from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


@dataclass
class MemoryAcquisitionPolicy:
    enabled: bool = False
    provider: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Dict[str, Any] | None) -> "MemoryAcquisitionPolicy":
        value = value or {}
        options = value.get("options") or {}
        return cls(
            enabled=bool(value.get("enabled", False)),
            provider=str(value["provider"]) if value.get("provider") else None,
            options=dict(options) if isinstance(options, dict) else {},
        )


@dataclass
class MemoryAcquisitionResult:
    status: str
    image_path: Optional[Path] = None
    provider: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class MemoryAcquisitionProvider(Protocol):
    def acquire(
        self,
        *,
        destination: Path,
        options: Dict[str, Any],
    ) -> MemoryAcquisitionResult:
        ...
