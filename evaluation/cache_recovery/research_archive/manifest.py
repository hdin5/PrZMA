from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .definition import (
    BehaviorDefinition,
    Classification,
    ParameterVariant,
    ResourceDefinition,
    SiteDefinition,
)


@dataclass(frozen=True)
class ResourceSpec:
    resource_id: str
    variant_id: str
    classification: Classification
    journey: str
    trigger: str
    request_order: int
    url: str
    url_pattern: str
    method: str
    status: int
    media_type: str
    content_encoding: str
    decoded_size: int
    encoded_size: int
    decoded_sha256: str
    encoded_sha256: str
    cache_headers: tuple[tuple[str, str], ...]
    parameter_variant: tuple[tuple[str, str], ...]
    cache_eligible: bool = True

    @property
    def etag(self) -> str:
        return dict(self.cache_headers)["ETag"]

    @property
    def last_modified(self) -> str:
        return dict(self.cache_headers)["Last-Modified"]

    @property
    def cache_control(self) -> str:
        return dict(self.cache_headers)["Cache-Control"]


BehaviorSpec = BehaviorDefinition


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _etag(encoded_body: bytes) -> str:
    return f'"sha256-{_sha256(encoded_body)}"'


def build_resource_spec(
    definition: SiteDefinition,
    resource: ResourceDefinition,
    variant: ParameterVariant,
) -> ResourceSpec:
    parameters = variant.as_dict()
    decoded = resource.decoded_body(definition.root, parameters)
    encoded = resource.encoded_body(decoded)
    headers = (
        ("Cache-Control", resource.cache_control),
        ("ETag", _etag(encoded)),
        ("Last-Modified", definition.last_modified),
        ("Content-Encoding", resource.content_encoding),
        ("Vary", "Accept-Encoding"),
    )
    pattern = resource.path
    if resource.parameter_rules:
        pattern += "?" + "&".join(
            f"{rule.name}={{{rule.name}}}" for rule in resource.parameter_rules
        )
    return ResourceSpec(
        resource_id=resource.resource_id,
        variant_id=variant.variant_id,
        classification=resource.classification,
        journey=resource.journey,
        trigger=resource.trigger,
        request_order=resource.request_order,
        url=variant.url(resource.path),
        url_pattern=pattern,
        method=resource.method,
        status=resource.status,
        media_type=resource.media_type,
        content_encoding=resource.content_encoding,
        decoded_size=len(decoded),
        encoded_size=len(encoded),
        decoded_sha256=_sha256(decoded),
        encoded_sha256=_sha256(encoded),
        cache_headers=headers,
        parameter_variant=variant.parameters,
        cache_eligible=resource.cache_eligible,
    )


def resource_specs(
    definition: Optional[SiteDefinition] = None,
) -> tuple[ResourceSpec, ...]:
    selected = definition or _default_definition()
    return tuple(
        build_resource_spec(selected, resource, variant)
        for resource in selected.resources
        for variant in resource.variants
    )


def behavior_specs(
    definition: Optional[SiteDefinition] = None,
) -> tuple[BehaviorSpec, ...]:
    selected = definition or _default_definition()
    return selected.behaviors


def _default_definition() -> SiteDefinition:
    from .sites.research_archive import research_archive_definition

    return research_archive_definition()


def write_public_manifests(
    root: Path,
    definition: Optional[SiteDefinition] = None,
) -> dict[str, Path]:
    selected = definition or _default_definition()
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    resource_path = destination / "resource_manifest.json"
    behavior_path = destination / "behavior_manifest.json"
    resources_payload = {
        "schema_version": 2,
        "site_id": selected.site_id,
        "site_version": selected.version,
        "resources": [asdict(item) for item in resource_specs(selected)],
    }
    behaviors_payload = {
        "schema_version": 2,
        "site_id": selected.site_id,
        "site_version": selected.version,
        "behaviors": [asdict(item) for item in behavior_specs(selected)],
    }
    resource_path.write_text(
        json.dumps(resources_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    behavior_path.write_text(
        json.dumps(behaviors_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"resources": resource_path, "behaviors": behavior_path}
