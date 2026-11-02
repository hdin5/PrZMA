from __future__ import annotations


SITE_NAME = "Research Archive"
SITE_SUBTITLE = "Open Research Collections and Data"


COLLECTIONS = (
    {
        "slug": "environmental-signals",
        "title": "Environmental Signals",
        "summary": "Field imagery, measurement series, and research notes.",
    },
    {
        "slug": "built-worlds",
        "title": "Built Worlds",
        "summary": "Urban systems, mobility data, and visual studies.",
    },
    {
        "slug": "digital-culture",
        "title": "Digital Culture",
        "summary": "Artifacts, methods, and contemporary media research.",
    },
)


SEARCH_RECORDS = (
    {
        "id": "report-web-evidence",
        "title": "Preserving web evidence in changing information environments",
        "summary": "Methods for collecting and documenting volatile public information.",
        "topic": "media",
        "year": "2026",
        "format": "report",
        "href": "/reports/web-evidence",
        "keywords": ("web", "evidence", "preservation", "public", "methods"),
    },
    {
        "id": "collection-environmental-signals",
        "title": "Environmental Signals",
        "summary": "Field imagery and observations from changing coastal environments.",
        "topic": "environment",
        "year": "2026",
        "format": "image",
        "href": "/collections/environmental-signals",
        "keywords": ("coastal", "environment", "signals", "field", "images"),
    },
    {
        "id": "dataset-regional-observations",
        "title": "Regional Observation Series",
        "summary": "Structured regional measurements with documented collection methods.",
        "topic": "environment",
        "year": "2026",
        "format": "dataset",
        "href": "/datasets/regional-observations",
        "keywords": ("coastal", "regional", "observations", "data", "measurements"),
    },
    {
        "id": "collection-built-worlds",
        "title": "Built Worlds",
        "summary": "Mobility systems, public space, and infrastructure observations.",
        "topic": "cities",
        "year": "2025",
        "format": "image",
        "href": "/collections",
        "keywords": ("city", "mobility", "urban", "infrastructure"),
    },
)


OBSERVATIONS = (
    {"site": "North Coast", "region": "north-coast", "date": "2026-03-12", "temperature": 12.8, "salinity": 31.4, "turbidity": 2.1},
    {"site": "North Coast", "region": "north-coast", "date": "2026-04-16", "temperature": 14.2, "salinity": 31.1, "turbidity": 2.4},
    {"site": "Estuary", "region": "estuary", "date": "2026-03-14", "temperature": 13.6, "salinity": 24.8, "turbidity": 5.7},
    {"site": "Estuary", "region": "estuary", "date": "2026-04-18", "temperature": 15.1, "salinity": 23.9, "turbidity": 6.2},
    {"site": "Harbor", "region": "harbor", "date": "2026-03-11", "temperature": 13.1, "salinity": 29.7, "turbidity": 3.8},
    {"site": "Harbor", "region": "harbor", "date": "2026-04-15", "temperature": 14.7, "salinity": 29.2, "turbidity": 4.0},
)


def collection_metadata() -> dict[str, object]:
    return {
        "collection": "Environmental Signals",
        "identifier": "RA-COL-ENV-04",
        "coverage": "2019-2026",
        "methods": ("field photography", "sensor observations", "quality review"),
        "curator": "Environmental Records Group",
    }


def search_results(
    *,
    query: str,
    topic: str,
    year: str,
    format_name: str,
    sort: str,
) -> list[dict[str, str]]:
    terms = {term for term in query.lower().split() if term}
    matches: list[dict[str, str]] = []
    for record in SEARCH_RECORDS:
        haystack = " ".join(
            (record["title"], record["summary"], *record["keywords"])
        ).lower()
        if terms and not any(term in haystack for term in terms):
            continue
        if topic != "all" and record["topic"] != topic:
            continue
        if year != "all" and record["year"] != year:
            continue
        if format_name != "all" and record["format"] != format_name:
            continue
        matches.append({key: str(value) for key, value in record.items() if key != "keywords"})
    if sort == "newest":
        matches.sort(key=lambda item: (item["year"], item["title"]), reverse=True)
    elif sort == "title":
        matches.sort(key=lambda item: item["title"])
    return matches


def dataset_preview(*, region: str, measure: str) -> list[dict[str, object]]:
    rows = OBSERVATIONS if region == "all" else tuple(
        row for row in OBSERVATIONS if row["region"] == region
    )
    return [
        {
            "site": row["site"],
            "region": row["region"],
            "date": row["date"],
            "measure": measure,
            "value": row[measure],
            "unit": "deg C" if measure == "temperature" else ("PSU" if measure == "salinity" else "NTU"),
        }
        for row in rows
    ]


def observation_sites() -> list[dict[str, object]]:
    return [
        {"id": "north-coast", "name": "North Coast", "x": 25, "y": 28, "latest": "12.8 deg C"},
        {"id": "estuary", "name": "Estuary", "x": 52, "y": 52, "latest": "24.8 PSU"},
        {"id": "harbor", "name": "Harbor", "x": 79, "y": 31, "latest": "3.8 NTU"},
    ]
