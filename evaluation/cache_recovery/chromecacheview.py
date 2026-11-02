from __future__ import annotations

import csv
import hashlib
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit


EXPORT_COLUMNS = (
    "URL",
    "Content Type",
    "File Size",
    "Cache Name",
    "Content Encoding",
    "Server Response",
    "Web Site",
    "Frame",
)


def normalize_media_type(value: str) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _integer(value: str) -> Optional[int]:
    text = str(value or "").replace(",", "").strip()
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class CacheRecord:
    url: str
    content_type: str
    file_size: Optional[int]
    cache_name: str = ""
    content_encoding: str = ""
    server_response: str = ""
    web_site: str = ""
    frame: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ToolRun:
    executable: str
    executable_sha256: str
    csv_path: str
    extract_dir: str
    record_count: int
    export_returncode: int
    copy_returncode: int


def parse_cache_csv(path: Path) -> List[CacheRecord]:
    records: List[CacheRecord] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            normalized = {str(key or "").strip(): str(value or "") for key, value in row.items()}
            records.append(
                CacheRecord(
                    url=normalized.get("URL", "").strip(),
                    content_type=normalize_media_type(normalized.get("Content Type", "")),
                    file_size=_integer(normalized.get("File Size", "")),
                    cache_name=normalized.get("Cache Name", "").strip(),
                    content_encoding=normalized.get("Content Encoding", "").strip().lower(),
                    server_response=normalized.get("Server Response", "").strip(),
                    web_site=normalized.get("Web Site", "").strip(),
                    frame=normalized.get("Frame", "").strip(),
                )
            )
    return records


def build_export_command(exe: Path, cache_dir: Path, csv_path: Path) -> List[str]:
    return [
        str(Path(exe).resolve()),
        "-folder",
        str(Path(cache_dir).resolve()),
        "/scomma",
        str(Path(csv_path).resolve()),
        "/Columns",
        ",".join(EXPORT_COLUMNS),
    ]


def build_copy_command(exe: Path, cache_dir: Path, extract_dir: Path, url_prefix: str) -> List[str]:
    return [
        str(Path(exe).resolve()),
        "-folder",
        str(Path(cache_dir).resolve()),
        "/copycache",
        url_prefix,
        "",
        "/CopyFilesFolder",
        str(Path(extract_dir).resolve()),
        "/UseWebSiteDirStructure",
        "1",
        "/NewNameIfExist",
        "1",
    ]


def query_collision_records(records: List[CacheRecord]) -> List[CacheRecord]:
    by_path: dict[tuple[str, str, str], dict[str, CacheRecord]] = defaultdict(dict)
    for record in records:
        parsed = urlsplit(record.url)
        if not parsed.scheme or not parsed.netloc:
            continue
        path_key = (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/")
        by_path[path_key].setdefault(record.url, record)
    collision_urls = {
        url
        for variants in by_path.values()
        if len(variants) > 1
        for url in variants
    }
    selected: List[CacheRecord] = []
    seen = set()
    for record in records:
        if record.url in collision_urls and record.url not in seen:
            selected.append(record)
            seen.add(record.url)
    return selected


def executable_sha256(exe: Path) -> str:
    digest = hashlib.sha256()
    with Path(exe).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_chromecacheview(
    exe: Path,
    cache_dir: Path,
    csv_path: Path,
    extract_dir: Path,
    *,
    url_prefix: str,
    timeout_sec: int = 120,
) -> ToolRun:
    exe = Path(exe)
    cache_dir = Path(cache_dir)
    csv_path = Path(csv_path)
    extract_dir = Path(extract_dir)
    if not exe.is_file():
        raise FileNotFoundError(f"ChromeCacheView executable not found: {exe}")
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"Chrome cache directory not found: {cache_dir}")
    if csv_path.exists():
        raise FileExistsError(f"Refusing to overwrite tool export: {csv_path}")
    if extract_dir.exists() and any(extract_dir.iterdir()):
        raise FileExistsError(f"Refusing to reuse nonempty extraction directory: {extract_dir}")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    exported = subprocess.run(
        build_export_command(exe, cache_dir, csv_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if exported.returncode != 0 or not csv_path.is_file():
        error = (exported.stderr or exported.stdout or "missing CSV output").strip()[:500]
        raise RuntimeError(f"ChromeCacheView export failed ({exported.returncode}): {error}")

    records = parse_cache_csv(csv_path)
    copied = subprocess.run(
        build_copy_command(exe, cache_dir, extract_dir, url_prefix),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if copied.returncode != 0:
        error = (copied.stderr or copied.stdout or "unknown error").strip()[:500]
        raise RuntimeError(f"ChromeCacheView copy failed ({copied.returncode}): {error}")

    for record in query_collision_records(records):
        record_key = hashlib.sha256(record.url.encode("utf-8")).hexdigest()[:20]
        record_dir = extract_dir / "_query_variants" / record_key
        record_dir.mkdir(parents=True, exist_ok=False)
        targeted = subprocess.run(
            build_copy_command(exe, cache_dir, record_dir, record.url),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if targeted.returncode != 0:
            error = (targeted.stderr or targeted.stdout or "unknown error").strip()[:500]
            raise RuntimeError(
                f"ChromeCacheView query-variant copy failed ({targeted.returncode}): {error}"
            )

    return ToolRun(
        executable=str(exe.resolve()),
        executable_sha256=executable_sha256(exe),
        csv_path=str(csv_path.resolve()),
        extract_dir=str(extract_dir.resolve()),
        record_count=len(records),
        export_returncode=exported.returncode,
        copy_returncode=copied.returncode,
    )
