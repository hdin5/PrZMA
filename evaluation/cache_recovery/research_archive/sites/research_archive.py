from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..content import collection_metadata, dataset_preview, observation_sites, search_results
from ..definition import (
    BehaviorDefinition,
    ParameterRule,
    ParameterVariant,
    ResourceDefinition,
    SiteDefinition,
)


SITE_ROOT = Path(__file__).resolve().parents[1]


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _collection_payload(_parameters: Mapping[str, str]) -> bytes:
    return _json_bytes(collection_metadata())


def _search_payload(parameters: Mapping[str, str]) -> bytes:
    results = search_results(
        query=parameters["query"],
        topic=parameters["topic"],
        year=parameters["year"],
        format_name=parameters["format"],
        sort=parameters["sort"],
    )
    return _json_bytes(
        {
            "parameters": dict(parameters),
            "count": len(results),
            "results": results,
        }
    )


def _dataset_payload(parameters: Mapping[str, str]) -> bytes:
    return _json_bytes(
        {
            "parameters": dict(parameters),
            "rows": dataset_preview(
                region=parameters["region"],
                measure=parameters["measure"],
            ),
        }
    )


def _map_payload(_parameters: Mapping[str, str]) -> bytes:
    return _json_bytes({"sites": observation_sites(), "updated": "2026-04-18"})


def _file(
    resource_id: str,
    classification: str,
    journey: str,
    trigger: str,
    request_order: int,
    path: str,
    relative_path: str,
    media_type: str,
    *,
    cache_control: str = "public, max-age=3600",
    parameter_rules: tuple[ParameterRule, ...] = (),
    variants: tuple[ParameterVariant, ...] = (ParameterVariant("default"),),
) -> ResourceDefinition:
    return ResourceDefinition(
        resource_id=resource_id,
        classification=classification,  # type: ignore[arg-type]
        journey=journey,
        trigger=trigger,
        request_order=request_order,
        path=path,
        relative_path=relative_path,
        media_type=media_type,
        cache_control=cache_control,
        parameter_rules=parameter_rules,
        variants=variants,
    )


def research_archive_definition() -> SiteDefinition:
    search_rules = (
        ParameterRule("query", required=True, free_text=True, max_length=80),
        ParameterRule("topic", default="all", allowed_values=("all", "environment", "cities", "media")),
        ParameterRule("year", default="all", allowed_values=("all", "2024", "2025", "2026")),
        ParameterRule("format", default="all", allowed_values=("all", "report", "dataset", "image")),
        ParameterRule("sort", default="relevance", allowed_values=("relevance", "newest", "title")),
    )
    search_variants = (
        ParameterVariant(
            "coastal-report",
            (("query", "coastal"), ("topic", "environment"), ("year", "2026"), ("format", "report"), ("sort", "newest")),
        ),
        ParameterVariant(
            "urban-data",
            (("query", "transit"), ("topic", "cities"), ("year", "2025"), ("format", "dataset"), ("sort", "title")),
        ),
        ParameterVariant(
            "media-images",
            (("query", "archives"), ("topic", "media"), ("year", "2024"), ("format", "image"), ("sort", "relevance")),
        ),
    )
    dataset_rules = (
        ParameterRule("region", required=True, allowed_values=("all", "north-coast", "estuary", "harbor")),
        ParameterRule("measure", required=True, allowed_values=("temperature", "salinity", "turbidity")),
    )
    dataset_variants = (
        ParameterVariant("all-temperature", (("region", "all"), ("measure", "temperature"))),
        ParameterVariant("estuary-salinity", (("region", "estuary"), ("measure", "salinity"))),
        ParameterVariant("harbor-turbidity", (("region", "harbor"), ("measure", "turbidity"))),
    )
    search_page_rules = (ParameterRule("query", default="", free_text=True, max_length=80),)
    resources = (
        _file("passive.home_html", "passive", "entry", "entry_navigation", 1, "/", "templates/home.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("passive.base_css", "passive", "entry", "entry_navigation", 2, "/static/css/base.css", "static/css/base.css", "text/css; charset=utf-8"),
        _file("passive.base_js", "passive", "entry", "entry_navigation", 2, "/static/js/base.js", "static/js/base.js", "application/javascript; charset=utf-8"),
        _file("passive.identity_png", "passive", "entry", "entry_navigation", 2, "/static/images/archive-identity.png", "static/images/archive-identity.png", "image/png"),
        _file("discover.html.01", "interactive", "discover", "navigation_01", 1, "/collections", "templates/collections.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("discover.css.01", "interactive", "discover", "navigation_01", 2, "/static/css/collections.css", "static/css/collections.css", "text/css; charset=utf-8"),
        _file("discover.html.02", "interactive", "discover", "navigation_02", 1, "/collections/environmental-signals", "templates/collection_detail.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("discover.js.01", "interactive", "discover", "navigation_02", 2, "/static/js/collections.js", "static/js/collections.js", "application/javascript; charset=utf-8"),
        ResourceDefinition("discover.json.01", "interactive", "discover", "dynamic_request_01", 1, "/api/collections/environmental-signals", "application/json; charset=utf-8", payload_factory=_collection_payload),
        _file("discover.webp.01", "interactive", "discover", "viewport_reveal_01", 1, "/static/images/coastal-observation.webp", "static/images/coastal-observation.webp", "image/webp"),
        _file("discover.jpeg.01", "interactive", "discover", "viewport_reveal_01", 2, "/static/images/field-research.jpg", "static/images/field-research.jpg", "image/jpeg"),
        _file("discover.png.01", "interactive", "discover", "viewport_reveal_01", 3, "/static/images/sensor-array.png", "static/images/sensor-array.png", "image/png"),
        _file("discover.html.03", "interactive", "discover", "embedded_open_01", 1, "/embed/map", "templates/map_embed.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("discover.css.02", "interactive", "discover", "embedded_open_01", 2, "/static/css/map.css", "static/css/map.css", "text/css; charset=utf-8"),
        _file("discover.js.02", "interactive", "discover", "embedded_open_01", 2, "/static/js/map.js", "static/js/map.js", "application/javascript; charset=utf-8"),
        ResourceDefinition("discover.json.02", "interactive", "discover", "embedded_open_01", 3, "/api/map/observations", "application/json; charset=utf-8", payload_factory=_map_payload),
        _file("discover.html.04", "interactive", "discover", "navigation_03", 1, "/map", "templates/map.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("inquiry.html.01", "interactive", "inquiry", "submit_01", 1, "/search", "templates/search.html", "text/html; charset=utf-8", cache_control="public, max-age=300", parameter_rules=search_page_rules, variants=(ParameterVariant("coastal", (("query", "coastal"),)),)),
        _file("inquiry.css.01", "interactive", "inquiry", "submit_01", 2, "/static/css/search.css", "static/css/search.css", "text/css; charset=utf-8"),
        _file("inquiry.js.01", "interactive", "inquiry", "submit_01", 2, "/static/js/search.js", "static/js/search.js", "application/javascript; charset=utf-8"),
        ResourceDefinition("inquiry.json.01", "interactive", "inquiry", "parameter_commit_01", 1, "/api/search", "application/json; charset=utf-8", payload_factory=_search_payload, parameter_rules=search_rules, variants=search_variants),
        _file("retrieve.html.01", "interactive", "retrieve", "navigation_01", 1, "/reports/web-evidence", "templates/report.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("retrieve.css.01", "interactive", "retrieve", "navigation_01", 2, "/static/css/report.css", "static/css/report.css", "text/css; charset=utf-8"),
        _file("retrieve.js.01", "interactive", "retrieve", "navigation_01", 2, "/static/js/report.js", "static/js/report.js", "application/javascript; charset=utf-8"),
        _file("retrieve.jpeg.01", "interactive", "retrieve", "navigation_01", 2, "/static/images/web-evidence-figure.jpg", "static/images/web-evidence-figure.jpg", "image/jpeg"),
        _file("retrieve.pdf.01", "interactive", "retrieve", "inline_open_01", 1, "/documents/web-evidence.pdf", "static/documents/web-evidence.pdf", "application/pdf"),
        _file("retrieve.pdf.02", "interactive", "retrieve", "download_01", 1, "/documents/methods-supplement.pdf", "static/documents/methods-supplement.pdf", "application/pdf"),
        _file("retrieve.html.02", "interactive", "retrieve", "navigation_02", 1, "/datasets/regional-observations", "templates/dataset.html", "text/html; charset=utf-8", cache_control="public, max-age=300"),
        _file("retrieve.css.02", "interactive", "retrieve", "navigation_02", 2, "/static/css/dataset.css", "static/css/dataset.css", "text/css; charset=utf-8"),
        _file("retrieve.js.02", "interactive", "retrieve", "navigation_02", 2, "/static/js/dataset.js", "static/js/dataset.js", "application/javascript; charset=utf-8"),
        ResourceDefinition("retrieve.json.01", "interactive", "retrieve", "parameter_request_01", 1, "/api/datasets/regional-observations", "application/json; charset=utf-8", payload_factory=_dataset_payload, parameter_rules=dataset_rules, variants=dataset_variants),
        _file("retrieve.csv.01", "interactive", "retrieve", "download_02", 1, "/downloads/regional-observations.csv", "static/data/regional-observations.csv", "text/csv; charset=utf-8"),
        _file("retrieve.json.02", "interactive", "retrieve", "download_03", 1, "/downloads/regional-observations.json", "static/data/regional-observations.json", "application/json; charset=utf-8"),
    )
    behaviors = (
        BehaviorDefinition("discover.navigation.01", "discover", "Open a discovery index", ("discover.html.01", "discover.css.01")),
        BehaviorDefinition("discover.navigation.02", "discover", "Open one representative detail document", ("discover.html.02", "discover.css.01", "discover.js.01")),
        BehaviorDefinition("discover.dynamic_request.01", "discover", "Request structured metadata from a visible control", ("discover.json.01",)),
        BehaviorDefinition("discover.viewport_reveal.01", "discover", "Reveal the complete progressive image set", ("discover.webp.01", "discover.jpeg.01", "discover.png.01")),
        BehaviorDefinition("discover.embedded_open.01", "discover", "Open and load an embedded interactive view", ("discover.html.03", "discover.css.02", "discover.js.02", "discover.json.02")),
        BehaviorDefinition("discover.navigation.03", "discover", "Open an independent visual exploration document", ("discover.html.04", "discover.css.02")),
        BehaviorDefinition("inquiry.submit.01", "inquiry", "Submit a non-empty inquiry", ("inquiry.html.01", "inquiry.css.01", "inquiry.js.01")),
        BehaviorDefinition("inquiry.parameter_commit.01", "inquiry", "Commit a non-default filter or ordering parameter", ("inquiry.json.01",)),
        BehaviorDefinition("retrieve.navigation.01", "retrieve", "Open a document detail view", ("retrieve.html.01", "retrieve.css.01", "retrieve.js.01", "retrieve.jpeg.01")),
        BehaviorDefinition("retrieve.inline_open.01", "retrieve", "Open an inline document representation", ("retrieve.pdf.01",)),
        BehaviorDefinition("retrieve.download.01", "retrieve", "Acquire a supporting document", ("retrieve.pdf.02",)),
        BehaviorDefinition("retrieve.navigation.02", "retrieve", "Open a structured-data detail view", ("retrieve.html.02", "retrieve.css.02", "retrieve.js.02")),
        BehaviorDefinition("retrieve.parameter_request.01", "retrieve", "Request a parameterized structured preview", ("retrieve.json.01",)),
        BehaviorDefinition("retrieve.download.02", "retrieve", "Acquire a tabular representation", ("retrieve.csv.01",)),
        BehaviorDefinition("retrieve.download.03", "retrieve", "Acquire a structured representation", ("retrieve.json.02",)),
    )
    return SiteDefinition(
        site_id="research-archive",
        service_name="Research Archive",
        version="2026-07-18",
        entry_path="/",
        resources=resources,
        behaviors=behaviors,
        root=SITE_ROOT,
    )
