from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, Optional
from urllib.parse import urlencode


Classification = Literal["passive", "interactive"]
PayloadFactory = Callable[[Mapping[str, str]], bytes]


@dataclass(frozen=True)
class ParameterRule:
    name: str
    required: bool = False
    default: str = ""
    allowed_values: tuple[str, ...] = ()
    free_text: bool = False
    max_length: int = 120

    def normalize(self, raw_value: Optional[str]) -> str:
        value = self.default if raw_value is None else str(raw_value).strip()
        if self.required and not value:
            raise ValueError(f"Missing required parameter: {self.name}")
        if len(value) > self.max_length:
            raise ValueError(f"Parameter is too long: {self.name}")
        if self.allowed_values and value not in self.allowed_values:
            raise ValueError(f"Unsupported value for {self.name}: {value}")
        if not self.free_text and not self.allowed_values and value and value != self.default:
            raise ValueError(f"Parameter does not accept arbitrary values: {self.name}")
        return value


@dataclass(frozen=True)
class ParameterVariant:
    variant_id: str
    parameters: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, str]:
        return dict(self.parameters)

    def url(self, path: str) -> str:
        query = urlencode(self.parameters)
        return f"{path}?{query}" if query else path


@dataclass(frozen=True)
class ResourceDefinition:
    resource_id: str
    classification: Classification
    journey: str
    trigger: str
    request_order: int
    path: str
    media_type: str
    body: Optional[bytes] = field(default=None, repr=False)
    relative_path: Optional[str] = None
    payload_factory: Optional[PayloadFactory] = field(
        default=None,
        repr=False,
        compare=False,
    )
    method: str = "GET"
    status: int = 200
    content_encoding: str = "identity"
    cache_control: str = "public, max-age=3600"
    cache_eligible: bool = True
    parameter_rules: tuple[ParameterRule, ...] = ()
    variants: tuple[ParameterVariant, ...] = (ParameterVariant("default"),)

    def __post_init__(self) -> None:
        sources = sum(
            value is not None
            for value in (self.body, self.relative_path, self.payload_factory)
        )
        if sources != 1:
            raise ValueError(
                f"{self.resource_id} must define exactly one body source"
            )
        if not self.path.startswith("/"):
            raise ValueError(f"Resource path must be absolute: {self.path}")
        if self.request_order < 1:
            raise ValueError("request_order must be positive")
        if self.content_encoding not in {"identity", "gzip"}:
            raise ValueError(f"Unsupported content encoding: {self.content_encoding}")
        if not self.variants:
            raise ValueError(f"{self.resource_id} must define a canonical variant")

    def normalize_parameters(
        self,
        query: Mapping[str, list[str]],
    ) -> dict[str, str]:
        allowed_names = {rule.name for rule in self.parameter_rules}
        unexpected = set(query).difference(allowed_names)
        if unexpected:
            raise ValueError(
                "Unexpected parameter(s): " + ", ".join(sorted(unexpected))
            )
        return {
            rule.name: rule.normalize((query.get(rule.name) or [None])[0])
            for rule in self.parameter_rules
        }

    def decoded_body(self, root: Path, parameters: Mapping[str, str]) -> bytes:
        if self.body is not None:
            return self.body
        if self.relative_path is not None:
            path = (root / self.relative_path).resolve()
            resolved_root = root.resolve()
            if path != resolved_root and resolved_root not in path.parents:
                raise ValueError(f"Resource escapes site root: {self.relative_path}")
            return path.read_bytes()
        if self.payload_factory is None:
            raise RuntimeError(f"No payload factory for {self.resource_id}")
        return self.payload_factory(parameters)

    def encoded_body(self, decoded_body: bytes) -> bytes:
        if self.content_encoding == "identity":
            return decoded_body
        return gzip.compress(decoded_body, compresslevel=9, mtime=0)

    def variant_for(self, parameters: Mapping[str, str]) -> ParameterVariant:
        normalized = dict(parameters)
        for variant in self.variants:
            if variant.as_dict() == normalized:
                return variant
        return ParameterVariant(
            "runtime",
            tuple(sorted((str(key), str(value)) for key, value in normalized.items())),
        )


@dataclass(frozen=True)
class BehaviorDefinition:
    behavior_id: str
    journey: str
    effect: str
    evidence_resource_ids: tuple[str, ...]


@dataclass(frozen=True)
class SiteDefinition:
    site_id: str
    service_name: str
    version: str
    entry_path: str
    resources: tuple[ResourceDefinition, ...]
    behaviors: tuple[BehaviorDefinition, ...]
    root: Path = Path(".")
    last_modified: str = "Sat, 18 Jul 2026 00:00:00 GMT"

    def __post_init__(self) -> None:
        ids = [resource.resource_id for resource in self.resources]
        if len(ids) != len(set(ids)):
            raise ValueError("Resource IDs must be unique")
        routes = [(resource.method, resource.path) for resource in self.resources]
        if len(routes) != len(set(routes)):
            raise ValueError("Method/path routes must be unique")
        if not any(resource.path == self.entry_path for resource in self.resources):
            raise ValueError("entry_path must identify a resource")
        resource_ids = set(ids)
        for behavior in self.behaviors:
            if not set(behavior.evidence_resource_ids) <= resource_ids:
                raise ValueError(
                    f"Unknown behavior evidence resource: {behavior.behavior_id}"
                )

    def resource_for(self, method: str, path: str) -> Optional[ResourceDefinition]:
        normalized_method = method.upper()
        return next(
            (
                resource
                for resource in self.resources
                if resource.method == normalized_method and resource.path == path
            ),
            None,
        )

