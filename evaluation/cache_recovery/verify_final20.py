from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE = ROOT / "evaluation" / "evidence" / "final20"
LIVE_REQUIRED = (
    "actions.jsonl",
    "snapshot.zip",
    "archive_requests.jsonl",
    "chromecacheview.csv",
    "comparison.json",
    "run_manifest.json",
    "action_metrics.json",
    "interpreted_przma_config.json",
    "model_usage_summary.json",
    "site_status.json",
    "snapshot_manifest.json",
    "tool_run.json",
    "provenance/actions.summary.json",
    "provenance/model_usage.jsonl",
    "provenance/snapshot/engine_record.json",
    "provenance/snapshot/manifest.json",
    "provenance/snapshot/policy.json",
    "provenance/snapshot/vm_result.json",
    "provenance/snapshot/zip_meta.json",
)
LIVE_REQUIRED_DIRECTORIES = ("tmi", "extracted_cache")
REPLAY_REQUIRED = (
    "replay_actions.jsonl",
    "replay_manifest.json",
    "archive_requests.jsonl",
    "site_status.json",
)
EXPECTED_REPLAY_DIRECTORIES = tuple(
    f"run_final20_paired_replay_v2_20260720_{index:02d}"
    for index in range(1, 21)
)
FORBIDDEN_PATH_MARKERS = (
    r"c:\users\yhd",
    "przma26",
    r"evaluation\results",
)
SNAPSHOT_METADATA = (
    ("snapshot_manifest.json", False),
    ("provenance/snapshot/manifest.json", False),
    ("provenance/snapshot/vm_result.json", True),
)
DEPRECATED_SUMMARY_PATHS = (
    "summary/final20_appendix_tables.md",
    "summary/final20_replay_summary.csv",
    "summary/final20_replay_summary.json",
    "summary/final20_verification.json",
)
HASH_LINE = re.compile(r"^([0-9A-Fa-f]{64})[ \t]+[*]?(.+)$")


class VerificationError(ValueError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise VerificationError(f"Missing required evidence file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Invalid JSON evidence file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"Expected a JSON object: {path}")
    return value


def _directories(path: Path, label: str) -> list[Path]:
    if not path.is_dir():
        raise VerificationError(f"Missing {label} directory: {path}")
    directories = sorted(item for item in path.iterdir() if item.is_dir())
    if len(directories) != 20:
        raise VerificationError(
            f"Expected exactly 20 {label} directories, found {len(directories)}"
        )
    return directories


def _require_relative_file(run_dir: Path, relative: str) -> Path:
    path = run_dir.joinpath(*PurePosixPath(relative).parts)
    if not path.is_file():
        raise VerificationError(f"Missing required evidence file: {path}")
    return path


def _require_relative_directory(run_dir: Path, relative: str) -> Path:
    path = run_dir.joinpath(*PurePosixPath(relative).parts)
    if not path.is_dir():
        raise VerificationError(f"Missing required evidence directory: {path}")
    return path


def _safe_relative_posix_path(value: str, label: str) -> PurePosixPath:
    if "\\" in value:
        raise VerificationError(f"{label} must use forward slashes: {value}")
    raw_parts = value.split("/")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        raise VerificationError(f"{label} contains an absolute path: {value}")
    if not raw_parts or any(part == ".." for part in raw_parts):
        raise VerificationError(f"{label} contains a traversal path: {value}")
    if any(part in {"", "."} for part in raw_parts):
        raise VerificationError(f"{label} contains a non-normal path: {value}")
    return posix


def _safe_inventory_path(value: str) -> PurePosixPath:
    return _safe_relative_posix_path(value, "SHA256SUMS path")


def _verify_hash_inventory(evidence: Path) -> int:
    inventory = evidence / "SHA256SUMS"
    if not inventory.is_file():
        raise VerificationError(f"Missing SHA256SUMS: {inventory}")

    entries: dict[str, str] = {}
    ordered_paths: list[str] = []
    for line_number, raw_line in enumerate(
        inventory.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            raise VerificationError(f"Blank SHA256SUMS entry at line {line_number}")
        match = HASH_LINE.fullmatch(raw_line)
        if not match:
            raise VerificationError(f"Malformed SHA256SUMS entry at line {line_number}")
        digest, raw_path = match.groups()
        if digest != digest.lower():
            raise VerificationError(
                f"SHA256SUMS digest must be lowercase at line {line_number}"
            )
        normalized = _safe_inventory_path(raw_path.strip()).as_posix()
        if normalized == "SHA256SUMS":
            raise VerificationError("SHA256SUMS must not hash itself")
        if normalized in entries:
            raise VerificationError(f"Duplicate SHA256SUMS path: {normalized}")
        entries[normalized] = digest
        ordered_paths.append(normalized)

    if ordered_paths != sorted(ordered_paths):
        raise VerificationError(
            "SHA256SUMS paths must be lexicographically sorted"
        )

    actual = {
        path.relative_to(evidence).as_posix()
        for path in evidence.rglob("*")
        if path.is_file() and path != inventory
    }
    listed = set(entries)
    unlisted = sorted(actual - listed)
    missing = sorted(listed - actual)
    if unlisted:
        raise VerificationError(
            f"Evidence files missing from SHA256SUMS: {', '.join(unlisted[:3])}"
        )
    if missing:
        raise VerificationError(
            f"SHA256SUMS references missing files: {', '.join(missing[:3])}"
        )

    for relative, expected in sorted(entries.items()):
        path = evidence / Path(*PurePosixPath(relative).parts)
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected:
            raise VerificationError(f"SHA-256 mismatch: {relative}")
    return len(entries)


def _selected_runs(evidence: Path) -> list[str]:
    selection = _read_object(evidence / "selection.json")
    raw_ids = selection.get("included_run_ids")
    if not isinstance(raw_ids, list):
        raise VerificationError("selection.json included_run_ids must be an array")
    selected = [str(value).strip() for value in raw_ids]
    if len(selected) != 20 or len(set(selected)) != 20 or not all(selected):
        raise VerificationError(
            "selection.json must contain exactly 20 unique included_run_ids"
        )
    return selected


def _verify_published_population(evidence: Path, selected: list[str]) -> None:
    metrics = _read_object(evidence / "summary" / "final20_metrics.json")
    if metrics.get("valid_run_count") != 20:
        raise VerificationError("final20_metrics.json valid_run_count must be 20")
    published_ids = metrics.get("valid_run_ids")
    if not isinstance(published_ids, list) or published_ids != selected:
        raise VerificationError(
            "final20_metrics.json valid_run_ids do not match selection.json"
        )


def _verify_replay_coverage(
    evidence: Path,
    replay_dirs: list[Path],
    selected: list[str],
) -> int:
    selected_set = set(selected)
    manifests: list[tuple[str, str]] = []
    sources: list[str] = []
    for replay_dir in replay_dirs:
        manifest_path = _require_relative_file(replay_dir, "replay_manifest.json")
        manifest = _read_object(manifest_path)
        source_run_id = str(manifest.get("source_run_id") or "").strip()
        if source_run_id not in selected_set:
            raise VerificationError(
                f"Replay {replay_dir.name} points to non-selected source run "
                f"{source_run_id or '<missing>'}"
            )
        replay_run_id = str(manifest.get("run_id") or replay_dir.name).strip()
        if not replay_run_id:
            raise VerificationError(f"Replay {replay_dir.name} has no run_id")
        manifests.append((replay_run_id, source_run_id))
        sources.append(source_run_id)

    counts = Counter(sources)
    if set(counts) != selected_set or any(value != 1 for value in counts.values()):
        raise VerificationError(
            "Replay manifests must cover every selected source run exactly once"
        )

    metrics = _read_object(
        evidence / "summary" / "final20_paired_replay_metrics.json"
    )
    if metrics.get("pair_count") != 20:
        raise VerificationError(
            "final20_paired_replay_metrics.json pair_count must be 20"
        )
    pairs = metrics.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != 20:
        raise VerificationError(
            "final20_paired_replay_metrics.json must publish exactly 20 pairs"
        )
    published: list[tuple[str, str]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise VerificationError("Each published paired replay must be an object")
        published.append(
            (
                str(pair.get("run_id") or "").strip(),
                str(pair.get("source_run_id") or "").strip(),
            )
        )
    if Counter(published) != Counter(manifests):
        raise VerificationError(
            "Published paired replay coverage does not match replay manifests"
        )
    return len(pairs)


def _normalized_forbidden_scan(value: str) -> str:
    normalized = value.casefold()
    while "\\\\" in normalized:
        normalized = normalized.replace("\\\\", "\\")
    return normalized


def _walk_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                yield key
            yield from _walk_json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_strings(child)


def _decoded_json_values(path: Path, raw_text: str) -> Iterable[str]:
    if path.suffix.casefold() == ".json":
        try:
            yield from _walk_json_strings(json.loads(raw_text))
        except json.JSONDecodeError:
            return
    elif path.suffix.casefold() == ".jsonl":
        for raw_line in raw_text.splitlines():
            if not raw_line.strip():
                continue
            try:
                yield from _walk_json_strings(json.loads(raw_line))
            except json.JSONDecodeError:
                continue


def _raise_if_forbidden(value: str, relative: str) -> None:
    normalized = _normalized_forbidden_scan(value)
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in normalized:
            raise VerificationError(
                f"Forbidden path marker {marker!r} found in {relative}"
            )


def _verify_portable_content(evidence: Path) -> None:
    for path in sorted(item for item in evidence.rglob("*") if item.is_file()):
        relative = path.relative_to(evidence).as_posix()
        _raise_if_forbidden(relative, relative)
        raw_text = path.read_bytes().decode("utf-8", errors="ignore")
        _raise_if_forbidden(raw_text, relative)
        for value in _decoded_json_values(path, raw_text):
            _raise_if_forbidden(value, relative)


def _verify_exclusions(evidence: Path, live_dirs: list[Path]) -> None:
    for relative in DEPRECATED_SUMMARY_PATHS:
        if evidence.joinpath(*PurePosixPath(relative).parts).exists():
            raise VerificationError(f"Deprecated summary path: {relative}")

    for live_dir in live_dirs:
        snapshot_dir = live_dir / "snapshot"
        if snapshot_dir.is_dir():
            relative = snapshot_dir.relative_to(evidence).as_posix()
            raise VerificationError(f"Excluded evidence path: {relative}")

    database_suffixes = {".db", ".sqlite", ".sqlite3"}
    preview_parts = {"preview_logs", "preview-logs", "previewlogs"}
    for path in evidence.rglob("*"):
        relative = path.relative_to(evidence)
        parts = {part.casefold() for part in relative.parts}
        name = path.name.casefold()
        excluded = (
            name == "vm_connection.json"
            or (path.is_file() and ("stdout" in name or "stderr" in name))
            or (
                path.is_file()
                and path.suffix.casefold() in database_suffixes
                and "schema" in name
            )
            or bool(parts & preview_parts)
            or (path.is_file() and "preview" in name and name.endswith(".log"))
        )
        if excluded:
            raise VerificationError(
                f"Excluded evidence path: {relative.as_posix()}"
            )


def _verify_snapshot_bundle(evidence: Path, live_dir: Path) -> None:
    snapshot = _require_relative_file(live_dir, "snapshot.zip")
    zip_meta_path = _require_relative_file(
        live_dir,
        "provenance/snapshot/zip_meta.json",
    )
    zip_meta = _read_object(zip_meta_path)
    expected_zip_path = (
        f"evaluation/evidence/final20/live_runs/{live_dir.name}/snapshot.zip"
    )
    if zip_meta.get("path") != expected_zip_path:
        raise VerificationError(
            f"{zip_meta_path.name} path must be {expected_zip_path}"
        )

    try:
        with zipfile.ZipFile(snapshot) as archive:
            members = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"Invalid snapshot ZIP {snapshot}: {exc}") from exc

    for relative, nested_manifest in SNAPSHOT_METADATA:
        metadata_path = _require_relative_file(live_dir, relative)
        record = _read_object(metadata_path)
        manifest = record.get("manifest") if nested_manifest else record
        if not isinstance(manifest, dict):
            raise VerificationError(
                f"Expected manifest object in snapshot metadata: {metadata_path}"
            )
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise VerificationError(
                f"Expected artifacts array in snapshot metadata: {metadata_path}"
            )
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise VerificationError(
                    f"Expected artifact object at {metadata_path} index {index}"
                )
            stored_path = artifact.get("stored_path")
            if not isinstance(stored_path, str) or not stored_path:
                raise VerificationError(
                    f"Missing stored_path at {metadata_path} index {index}"
                )
            normalized = _safe_relative_posix_path(
                stored_path,
                f"{metadata_path.name} stored_path",
            )
            if not normalized.parts or normalized.parts[0] != "artifacts":
                raise VerificationError(
                    f"{metadata_path.name} stored_path must begin with artifacts/: "
                    f"{stored_path}"
                )
            member = normalized.as_posix()
            if member not in members:
                raise VerificationError(
                    f"{metadata_path.name} stored_path is not present in snapshot.zip: "
                    f"{member}"
                )


def verify_evidence(evidence: Path) -> dict[str, Any]:
    evidence = Path(evidence).resolve()
    if not evidence.is_dir():
        raise VerificationError(f"Evidence directory does not exist: {evidence}")

    selected = _selected_runs(evidence)
    live_dirs = _directories(evidence / "live_runs", "live-run")
    replay_dirs = _directories(evidence / "replay_runs", "replay-run")
    if {item.name for item in live_dirs} != set(selected):
        raise VerificationError(
            "Live-run directory names do not match selection.json"
        )
    if {item.name for item in replay_dirs} != set(EXPECTED_REPLAY_DIRECTORIES):
        raise VerificationError(
            "Replay-run directory names must be exactly "
            "run_final20_paired_replay_v2_20260720_01 through _20"
        )
    for live_dir in live_dirs:
        for relative in LIVE_REQUIRED:
            _require_relative_file(live_dir, relative)
        for relative in LIVE_REQUIRED_DIRECTORIES:
            _require_relative_directory(live_dir, relative)
        _verify_snapshot_bundle(evidence, live_dir)
    for replay_dir in replay_dirs:
        for relative in REPLAY_REQUIRED:
            _require_relative_file(replay_dir, relative)

    _verify_exclusions(evidence, live_dirs)
    _verify_portable_content(evidence)
    _verify_published_population(evidence, selected)
    pair_count = _verify_replay_coverage(evidence, replay_dirs, selected)
    hashed_file_count = _verify_hash_inventory(evidence)
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "selected_run_count": len(selected),
        "live_run_count": len(live_dirs),
        "replay_run_count": len(replay_dirs),
        "pair_count": pair_count,
        "hashed_file_count": hashed_file_count,
        "verification_scope": [
            "selected_population",
            "required_raw_evidence",
            "portable_metadata",
            "snapshot_archive_membership",
            "evidence_exclusions",
            "sha256_inventory",
            "published_pair_coverage",
        ],
        "statistical_intervals_recomputed": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify packaged PrZMA final-20 Cache Recovery evidence"
    )
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = verify_evidence(args.evidence)
    except (OSError, VerificationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
