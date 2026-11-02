from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evaluation.cache_recovery.runner import (
    ROOT,
    autonomous_framework_fingerprint,
    autonomous_freeze_gate_status,
)


DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "autonomous_freeze_manifest.json"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable_summary_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def create_freeze_manifest(
    pilot_summary: Path,
    output: Path,
    *,
    approve: bool,
) -> dict[str, Any]:
    if not approve:
        raise ValueError("--approve is required to create a Campaign freeze manifest")

    pilot_summary = Path(pilot_summary)
    if not pilot_summary.is_absolute():
        pilot_summary = ROOT / pilot_summary
    if not pilot_summary.is_file():
        raise FileNotFoundError(f"Pilot summary does not exist: {pilot_summary}")
    summary = _read_object(pilot_summary)
    if summary.get("ready_for_freeze_review") is not True:
        raise ValueError(
            "Pilot summary ready_for_freeze_review must be exactly true"
        )

    output = Path(output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = autonomous_framework_fingerprint()
    record = {
        "schema_version": "1.0.0",
        "user_approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "pilot_summary_path": _portable_summary_path(pilot_summary),
        "pilot_summary_sha256": _sha256(pilot_summary),
        "framework_fingerprint": fingerprint,
    }

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary_path = Path(handle.name)
        accepted, reason = autonomous_freeze_gate_status(
            temporary_path,
            expected_fingerprint=fingerprint,
        )
        if not accepted:
            raise ValueError(f"Generated freeze manifest failed validation: {reason}")
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return record


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the explicitly approved autonomous Campaign freeze manifest"
    )
    parser.add_argument("--pilot-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        record = create_freeze_manifest(
            args.pilot_summary,
            args.output,
            approve=args.approve,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "pilot_summary_sha256": record["pilot_summary_sha256"],
                "framework_fingerprint": record["framework_fingerprint"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
