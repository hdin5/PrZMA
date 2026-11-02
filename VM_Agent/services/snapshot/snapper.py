# VM_Agent/services/snapshot/snapper.py
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .acquisition import (
    AcquisitionOutcome,
    AcquisitionRequest,
    canonical_source_key,
    classify_os_error,
    legacy_request,
    validate_config_source,
)
from .windows_collectors import (
    EventLogCollector,
    RegistryCollector,
    UserContext,
    UserContextResolver,
)
from .vss import VssSnapshotError, VssSnapshotProvider
from .memory_provider import (
    MemoryAcquisitionPolicy,
    MemoryAcquisitionProvider,
)
from shared.wire_schemas import (
    SnapshotPolicy,
    SnapshotManifest,
    CollectedArtifact,
    sha256_file,
    utc_now_iso,
)

def _resolve_path_placeholders(p: str, meta: Dict[str, Any] | None = None) -> str:
    meta = meta or {}

    p = os.path.expandvars(p)

    env = os.environ
    explicit_user_profile = meta.get("USERPROFILE") or meta.get("user_profile")
    mapping: Dict[str, Optional[str]] = {
        "LOCALAPPDATA": meta.get("LOCALAPPDATA") or meta.get("localappdata") or env.get("LOCALAPPDATA"),
        "APPDATA": meta.get("APPDATA") or meta.get("appdata") or env.get("APPDATA"),
        "USERPROFILE": explicit_user_profile or env.get("USERPROFILE"),
        "WINDIR": env.get("WINDIR"),
        "SYSTEMDRIVE": env.get("SYSTEMDRIVE"),
        "PROGRAMDATA": env.get("PROGRAMDATA"),
        "PROFILE": meta.get("profile") or meta.get("PROFILE") or "Default",

        # Allow runtime override (critical)
        "CHROME_ROOT": (
            meta.get("CHROME_ROOT")
            or meta.get("chrome_root")
            or meta.get("BROWSER_ROOT")
            or meta.get("browser_root")
            or os.environ.get("PRZMA_CHROME_ROOT")
        ),
        "EDGE_ROOT": (
            meta.get("EDGE_ROOT")
            or meta.get("edge_root")
            or os.environ.get("PRZMA_EDGE_ROOT")
        ),
    }

    if explicit_user_profile:
        if not meta.get("APPDATA") and not meta.get("appdata"):
            mapping["APPDATA"] = os.path.join(str(explicit_user_profile), "AppData", "Roaming")
        if not meta.get("LOCALAPPDATA") and not meta.get("localappdata"):
            mapping["LOCALAPPDATA"] = os.path.join(str(explicit_user_profile), "AppData", "Local")

    if mapping.get("LOCALAPPDATA"):
        local = mapping["LOCALAPPDATA"]
        if not mapping.get("CHROME_ROOT"):
            mapping["CHROME_ROOT"] = os.path.join(local, "Google", "Chrome", "User Data")
        if not mapping.get("EDGE_ROOT"):
            mapping["EDGE_ROOT"] = os.path.join(local, "Microsoft", "Edge", "User Data")

    out = p
    for k, v in mapping.items():
        if v:
            out = out.replace("{" + k + "}", v)

    return out

def _expand_glob(p: str) -> List[str]:
    if "*" in p or "?" in p:
        import glob
        return glob.glob(p, recursive=True)
    return [p]

def _safe_relpath(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except Exception:
        return p.name


def _bounded_destination(
    *,
    layer_dir: Path,
    src: Path,
    base_dir: Optional[Path],
    max_absolute_chars: int = 239,
) -> Tuple[Path, Dict[str, Any]]:
    if base_dir is None:
        relative_path = src.name
    else:
        relative_path = _safe_relpath(src, base_dir)

    destination = layer_dir / Path(relative_path)
    absolute_destination = Path(os.path.abspath(str(destination)))
    if len(str(absolute_destination)) <= max_absolute_chars:
        return destination, {}

    suffix = src.suffix if len(src.suffix) <= 16 else ""
    path_digest = hashlib.sha256(
        relative_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return (
        layer_dir / "__long_paths__" / f"{path_digest}{suffix}",
        {
            "path_shortened": True,
            "source_relative_path": relative_path,
        },
    )


def _read_file_bytes_win32_shared(path: Path) -> Optional[bytes]:
    """Windows: read a file opened by another process using FILE_SHARE_READ (useful for cache index files)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x1
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        path_str = str(path.resolve())
        handle = kernel32.CreateFileW(
            path_str,
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return None
        try:
            size = path.stat().st_size
            if size <= 0:
                return b""
            buf = (ctypes.c_byte * size)()
            n = wintypes.DWORD()
            ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(n), None)
            if not ok:
                return None
            return bytes(buf[: n.value])
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _read_file_bytes_win32_robocopy_backup(path: Path) -> Optional[bytes]:
    """Windows: copy a locked file via robocopy /B (backup mode) then read it."""
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        import tempfile
        path = path.resolve()
        if not path.is_file():
            return None
        parent = path.parent
        name = path.name
        with tempfile.TemporaryDirectory(prefix="przma_robocopy_") as tmp:
            dest = Path(tmp)
            r = subprocess.run(
                ["robocopy", str(parent), str(dest), name, "/B", "/R:0", "/W:0"],
                capture_output=True,
                timeout=30,
                text=True,
            )
            if dest.joinpath(name).exists():
                return dest.joinpath(name).read_bytes()
    except Exception:
        pass
    return None


def _read_file_bytes(path: Path) -> Optional[bytes]:
    """Read file bytes. On Windows, retry with (1) shared read then (2) robocopy backup mode."""
    try:
        return path.read_bytes()
    except Exception:
        pass
    if sys.platform == "win32":
        out = _read_file_bytes_win32_shared(path)
        if out is not None:
            return out
        if "Cache_Data" in path.parts or (path.name in ("index", "index-dir") or path.name.startswith("data_")):
            return _read_file_bytes_win32_robocopy_backup(path)
    return None


def enumerate_directory_files(
    root: Path,
    *,
    max_files: Optional[int] = None,
) -> Tuple[List[Path], bool]:
    files: List[Path] = []
    for current_root, directory_names, file_names in os.walk(root):
        directory_names.sort(key=str.lower)
        file_names.sort(key=str.lower)
        for file_name in file_names:
            candidate = Path(current_root) / file_name
            if not candidate.is_file():
                continue
            if max_files is not None and len(files) >= max_files:
                return files, True
            files.append(candidate)
    return files, False


class Snapper:
    def __init__(
        self,
        staging_root: str = "snapshots_staging",
        *,
        registry_collector: Optional[RegistryCollector] = None,
        event_log_collector: Optional[EventLogCollector] = None,
        vss_provider_factory: Optional[Callable[[], VssSnapshotProvider]] = None,
        user_context_resolver: Optional[UserContextResolver] = None,
        memory_provider: Optional[MemoryAcquisitionProvider] = None,
    ):
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.registry_collector = registry_collector or RegistryCollector()
        self.event_log_collector = event_log_collector or EventLogCollector()
        self.vss_provider_factory = vss_provider_factory or VssSnapshotProvider
        self.user_context_resolver = user_context_resolver or UserContextResolver()
        self.memory_provider = memory_provider

    def collect(self, policy_dict: Dict[str, Any]) -> Dict[str, Any]:
        policy = SnapshotPolicy.from_dict(policy_dict)

        stage_key = hashlib.sha256(
            f"{policy.run_id}\0{policy.snapshot_id}".encode("utf-8")
        ).hexdigest()[:20]
        stage_dir = self.staging_root / f"snap_{stage_key}"
        stage_dir.mkdir(parents=True, exist_ok=True)

        collected: List[Dict[str, Any]] = []
        outcomes: List[Dict[str, Any]] = []
        total_bytes = 0
        deduplicated: Dict[str, Dict[str, Any]] = {}
        reusable_file_sources: Dict[
            str,
            Tuple[AcquisitionOutcome, List[Dict[str, Any]]],
        ] = {}
        status_counts: Dict[str, int] = {}
        method_counts: Dict[str, int] = {}
        vss_provider = self.vss_provider_factory()
        vss_cleanup: List[Dict[str, Any]] = []
        user_context = self._resolve_user_context(policy)

        layers = list(getattr(policy, "layers", []) or [])
        if not layers:
            layers = list((policy.layer_policies or {}).keys())

        try:
            for layer in layers:
                lp = (policy.layer_policies or {}).get(layer, {})
                include_paths = lp.get("include_paths", []) if isinstance(lp, dict) else []
                raw_requests = lp.get("requests", []) if isinstance(lp, dict) else []
                requests: List[AcquisitionRequest] = []
                if isinstance(raw_requests, list) and raw_requests:
                    for value in raw_requests:
                        if isinstance(value, dict):
                            requests.append(AcquisitionRequest.from_dict(value))
                else:
                    for raw in include_paths:
                        if isinstance(raw, str):
                            requests.append(legacy_request(layer, raw))

                max_file_mb = (
                    lp.get("max_file_mb") if isinstance(lp, dict) else None
                )
                max_total_mb = (
                    lp.get("max_total_mb") if isinstance(lp, dict) else None
                )
                max_files_value = (
                    lp.get("max_files") if isinstance(lp, dict) else None
                )
                max_file = int(float(max_file_mb if max_file_mb is not None else 200) * 1024 * 1024)
                max_total = int(float(max_total_mb if max_total_mb is not None else 1024) * 1024 * 1024)
                max_files = (
                    int(max_files_value)
                    if max_files_value is not None
                    else None
                )
                layer_state = {"bytes": 0, "files": 0}
                layer_meta = dict((lp.get("meta") if isinstance(lp, dict) else None) or {})
                if user_context.user_profile:
                    layer_meta.setdefault("USERPROFILE", user_context.user_profile)
                    layer_meta.setdefault("APPDATA", user_context.appdata)
                    layer_meta.setdefault("LOCALAPPDATA", user_context.localappdata)
                if user_context.user_sid:
                    layer_meta.setdefault("user_sid", user_context.user_sid)
                browser_name = str(layer_meta.get("browser") or "").lower()
                if user_context.localappdata and browser_name == "chrome":
                    layer_meta.setdefault(
                        "CHROME_ROOT",
                        os.path.join(
                            user_context.localappdata,
                            "Google",
                            "Chrome",
                            "User Data",
                        ),
                    )
                elif user_context.localappdata and browser_name == "edge":
                    layer_meta.setdefault(
                        "EDGE_ROOT",
                        os.path.join(
                            user_context.localappdata,
                            "Microsoft",
                            "Edge",
                            "User Data",
                        ),
                    )

                for request in requests:
                    try:
                        source_cache_key = self._file_source_cache_key(
                            request,
                            layer_meta,
                        )
                        cached_source = (
                            reusable_file_sources.get(source_cache_key)
                            if source_cache_key is not None
                            else None
                        )
                        if cached_source is not None:
                            outcome, new_artifacts = self._reuse_file_source(
                                request=request,
                                layer_meta=layer_meta,
                                cached_outcome=cached_source[0],
                                cached_artifacts=cached_source[1],
                            )
                            added_bytes = 0
                        else:
                            outcome, new_artifacts, added_bytes = self._collect_request(
                                request=request,
                                stage_dir=stage_dir,
                                layer_meta=layer_meta,
                                max_file=max_file,
                                max_total=max_total,
                                max_files=max_files,
                                layer_state=layer_state,
                                deduplicated=deduplicated,
                                vss_provider=vss_provider,
                            )
                            if (
                                source_cache_key is not None
                                and outcome.status in {"collected", "not_present"}
                            ):
                                reusable_file_sources[source_cache_key] = (
                                    outcome,
                                    list(new_artifacts),
                                )
                    except Exception as exc:
                        status, error_code = classify_os_error(exc)
                        outcome = AcquisitionOutcome(
                            artifact_key=request.artifact_key,
                            layer=request.layer,
                            source=request.source,
                            status=(
                                status
                                if status in {
                                    "permission_denied",
                                    "locked",
                                    "not_present",
                                }
                                else "acquisition_failed"
                            ),
                            availability=request.availability,
                            attempted_methods=[],
                            error_code=error_code,
                            message=str(exc),
                            meta=dict(request.meta),
                        )
                        new_artifacts = []
                        added_bytes = 0
                    for artifact in new_artifacts:
                        if artifact not in collected:
                            collected.append(artifact)
                    total_bytes += added_bytes
                    outcome_dict = outcome.to_dict()
                    outcomes.append(outcome_dict)
                    status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
                    for method in outcome.attempted_methods:
                        method_counts[method] = method_counts.get(method, 0) + 1
        finally:
            try:
                vss_cleanup = vss_provider.close_all()
            except Exception as exc:
                vss_cleanup = [
                    {
                        "shadow_id": None,
                        "volume": None,
                        "removed": False,
                        "error": str(exc),
                    }
                ]

        memory_environment = self._memory_environment(policy)
        if memory_environment["status"] == "unsupported":
            outcomes.append(
                AcquisitionOutcome(
                    artifact_key="memory.live_ram",
                    layer="memory_runtime",
                    source="memory_acquisition",
                    status="unsupported",
                    availability="required",
                    attempted_methods=[],
                    error_code=str(memory_environment["error_code"]),
                    message=str(memory_environment["message"]),
                ).to_dict()
            )
            status_counts["unsupported"] = status_counts.get("unsupported", 0) + 1

        vss_was_attempted = any(
            "vss" in (outcome.get("attempted_methods") or [])
            for outcome in outcomes
        )
        vss_cleanup_complete = (
            not vss_was_attempted
            or (
                bool(vss_cleanup)
                and all(
                    isinstance(item, dict)
                    and bool(item.get("removed"))
                    and not item.get("error")
                    for item in vss_cleanup
                )
            )
        )

        # zip
        zip_path = stage_dir.with_suffix(".zip")
        self._make_zip(stage_dir, zip_path)

        # manifest
        manifest = SnapshotManifest(
            run_id=policy.run_id,
            snapshot_id=policy.snapshot_id,
            agent_id=policy.agent_id,
            created_at=utc_now_iso(),
            trigger=policy.trigger or {},
            layers=layers,
            artifacts=collected,
            requests=outcomes,
            summary={
                "artifact_count": len(collected),
                "total_bytes": total_bytes,
                "complete": all(
                    outcome["status"] == "collected"
                    or (
                        outcome.get("availability") == "conditional"
                        and outcome["status"] == "not_present"
                    )
                    or (
                        outcome.get("availability") == "conditional"
                        and outcome["status"]
                        in {"partially_collected", "limit_exceeded"}
                        and bool((outcome.get("meta") or {}).get("bounded_collection"))
                        and bool((outcome.get("meta") or {}).get("truncated"))
                        and outcome.get("error_code")
                        in {"max_files", "max_file_mb", "max_total_mb"}
                    )
                    for outcome in outcomes
                ) and vss_cleanup_complete,
                "status_counts": status_counts,
                "method_counts": method_counts,
                "vss_cleanup_complete": vss_cleanup_complete,
                "zip_sha256": sha256_file(str(zip_path)) if zip_path.exists() else None,
            },
            environment={
                "vss": {"cleanup": vss_cleanup},
                "user_context": user_context.to_dict(),
                "memory_acquisition": memory_environment,
            },
            repro={},
            notes="",
        )

        zip_bytes = zip_path.read_bytes() if zip_path.exists() else b""

        return {
            "ok": True,
            "run_id": policy.run_id,
            "snapshot_id": policy.snapshot_id,
            "agent_id": policy.agent_id,
            "manifest": manifest.to_dict(),
            "zip_bytes": zip_bytes,
        }

    @staticmethod
    def _file_source_cache_key(
        request: AcquisitionRequest,
        layer_meta: Dict[str, Any],
    ) -> Optional[str]:
        if request.source_type != "file":
            return None
        source = _resolve_path_placeholders(request.source, layer_meta)
        if validate_config_source(source):
            return None
        return canonical_source_key(Path(source))

    @staticmethod
    def _reuse_file_source(
        *,
        request: AcquisitionRequest,
        layer_meta: Dict[str, Any],
        cached_outcome: AcquisitionOutcome,
        cached_artifacts: List[Dict[str, Any]],
    ) -> Tuple[AcquisitionOutcome, List[Dict[str, Any]]]:
        for artifact in cached_artifacts:
            keys = artifact.setdefault("meta", {}).setdefault("artifact_keys", [])
            if request.artifact_key not in keys:
                keys.append(request.artifact_key)

        source = _resolve_path_placeholders(request.source, layer_meta)
        outcome = AcquisitionOutcome(
            artifact_key=request.artifact_key,
            layer=request.layer,
            source=source,
            status=cached_outcome.status,
            availability=request.availability,
            attempted_methods=list(cached_outcome.attempted_methods),
            artifacts=[
                str(artifact.get("stored_path"))
                for artifact in cached_artifacts
                if artifact.get("stored_path")
            ],
            byte_count=cached_outcome.byte_count,
            error_code=cached_outcome.error_code,
            message=cached_outcome.message,
            meta={
                **request.meta,
                "reused_source": True,
                "reused_from_artifact_key": cached_outcome.artifact_key,
            },
        )
        return outcome, list(cached_artifacts)

    def _memory_environment(self, policy: SnapshotPolicy) -> Dict[str, Any]:
        memory_policy = MemoryAcquisitionPolicy.from_dict(
            getattr(policy, "memory_acquisition", None)
        )
        if not memory_policy.enabled:
            return {
                "enabled": False,
                "provider": memory_policy.provider,
                "status": "disabled",
                "error_code": None,
                "message": None,
            }
        if self.memory_provider is None:
            return {
                "enabled": True,
                "provider": memory_policy.provider,
                "status": "unsupported",
                "error_code": "memory_provider_not_configured",
                "message": (
                    "Live-memory acquisition was requested, but no approved "
                    "memory provider is configured."
                ),
            }
        return {
            "enabled": True,
            "provider": memory_policy.provider,
            "status": "provider_available",
            "error_code": None,
            "message": (
                "A provider is available, but live-memory acquisition is not "
                "executed by the current snapshot phase."
            ),
        }

    def _collect_request(
        self,
        *,
        request: AcquisitionRequest,
        stage_dir: Path,
        layer_meta: Dict[str, Any],
        max_file: int,
        max_total: int,
        max_files: Optional[int],
        layer_state: Dict[str, int],
        deduplicated: Dict[str, Dict[str, Any]],
        vss_provider: VssSnapshotProvider,
    ) -> Tuple[AcquisitionOutcome, List[Dict[str, Any]], int]:
        planned_methods = list(dict.fromkeys(request.strategy or ["direct"]))
        if request.source_type in {"registry", "event_log"}:
            native_outcome, native_artifacts, native_bytes = self._collect_native_request(
                request=request,
                stage_dir=stage_dir,
                max_file=max_file,
                max_total=max_total,
                layer_state=layer_state,
                layer_meta=layer_meta,
            )
            if native_outcome.status == "collected" or "vss" not in planned_methods:
                return native_outcome, native_artifacts, native_bytes

            fallback_paths = self._native_fallback_paths(
                request,
                native_outcome,
                layer_meta,
            )
            if not fallback_paths:
                return native_outcome, native_artifacts, native_bytes

            artifacts = list(native_artifacts)
            added_bytes = native_bytes
            attempted_methods = list(native_outcome.attempted_methods)
            fallback_outcomes: List[AcquisitionOutcome] = []
            for fallback_path in fallback_paths:
                fallback_request = AcquisitionRequest(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=fallback_path,
                    source_type="file",
                    strategy=["direct", "vss"],
                    availability=request.availability,
                    meta={**request.meta, "native_fallback": True},
                )
                fallback_outcome, fallback_artifacts, fallback_bytes = self._collect_request(
                    request=fallback_request,
                    stage_dir=stage_dir,
                    layer_meta=layer_meta,
                    max_file=max_file,
                    max_total=max_total,
                    max_files=max_files,
                    layer_state=layer_state,
                    deduplicated=deduplicated,
                    vss_provider=vss_provider,
                )
                fallback_outcomes.append(fallback_outcome)
                for artifact in fallback_artifacts:
                    if artifact not in artifacts:
                        artifacts.append(artifact)
                added_bytes += fallback_bytes
                for method in fallback_outcome.attempted_methods:
                    if method not in attempted_methods:
                        attempted_methods.append(method)

            all_recovered = bool(fallback_outcomes) and all(
                outcome.status == "collected" for outcome in fallback_outcomes
            )
            if all_recovered:
                status = "collected"
                error_code = None
                message = None
            elif artifacts:
                status = "partially_collected"
                error_code = native_outcome.error_code
                message = native_outcome.message
            else:
                failed = next(
                    (
                        outcome
                        for outcome in fallback_outcomes
                        if outcome.status != "collected"
                    ),
                    native_outcome,
                )
                status = failed.status
                error_code = failed.error_code
                message = failed.message

            stored_paths = list(
                dict.fromkeys(
                    path
                    for outcome in [native_outcome, *fallback_outcomes]
                    for path in outcome.artifacts
                )
            )
            logical_bytes = sum(
                int(outcome.byte_count)
                for outcome in [native_outcome, *fallback_outcomes]
            )
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=request.source,
                    status=status,
                    availability=request.availability,
                    attempted_methods=attempted_methods,
                    artifacts=stored_paths,
                    byte_count=logical_bytes,
                    error_code=error_code,
                    message=message,
                    meta={
                        **native_outcome.meta,
                        "fallback_outcomes": [
                            outcome.to_dict() for outcome in fallback_outcomes
                        ],
                    },
                ),
                artifacts,
                added_bytes,
            )
        if request.source_type != "file":
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=request.source,
                    status="unsupported",
                    availability=request.availability,
                    attempted_methods=[],
                    error_code="strategy_not_implemented",
                    message=f"Unsupported source type: {request.source_type}",
                ),
                [],
                0,
            )

        source = _resolve_path_placeholders(request.source, layer_meta)
        invalid_reason = validate_config_source(source)
        if invalid_reason:
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=source,
                    status="acquisition_failed",
                    availability=request.availability,
                    attempted_methods=[],
                    error_code="invalid_source",
                    message=invalid_reason,
                ),
                [],
                0,
            )

        expanded = [Path(value) for value in _expand_glob(source)]
        existing = [path for path in expanded if path.exists()]
        if not existing:
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=source,
                    status="not_present",
                    availability=request.availability,
                    attempted_methods=["direct"],
                    error_code="not_found",
                ),
                [],
                0,
            )

        files: List[Tuple[Path, Optional[Path]]] = []
        enumeration_truncated = False
        for path in existing:
            if path.is_file():
                files.append((path, None))
            elif path.is_dir():
                remaining_files = (
                    max(max_files - layer_state["files"] - len(files), 0)
                    if max_files is not None
                    else None
                )
                directory_files, truncated = enumerate_directory_files(
                    path,
                    max_files=remaining_files,
                )
                files.extend((candidate, path) for candidate in directory_files)
                if truncated:
                    enumeration_truncated = True
                    break

        if not files and enumeration_truncated:
            outcome_meta = dict(request.meta)
            outcome_meta["truncated"] = True
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=source,
                    status="limit_exceeded",
                    availability=request.availability,
                    attempted_methods=["direct"],
                    error_code="max_files",
                    message=f"Layer file limit {max_files} was reached",
                    meta=outcome_meta,
                ),
                [],
                0,
            )

        if not files:
            return (
                AcquisitionOutcome(
                    artifact_key=request.artifact_key,
                    layer=request.layer,
                    source=source,
                    status="not_present",
                    availability=request.availability,
                    attempted_methods=["direct"],
                    error_code="empty_source",
                ),
                [],
                0,
            )

        artifacts: List[Dict[str, Any]] = []
        stored_paths: List[str] = []
        logical_bytes = 0
        added_bytes = 0
        issues: List[Tuple[str, str, str]] = []
        if enumeration_truncated:
            issues.append(
                (
                    "limit_exceeded",
                    "max_files",
                    f"Layer file limit {max_files} was reached",
                )
            )
        attempted_methods: List[str] = ["direct"]

        for file_path, base_dir in files:
            if (
                max_files is not None
                and layer_state["files"] >= max_files
            ):
                issues.append(
                    (
                        "limit_exceeded",
                        "max_files",
                        f"Layer file limit {max_files} was reached",
                    )
                )
                break
            try:
                size = file_path.stat().st_size
            except Exception as exc:
                status, code = classify_os_error(exc)
                issues.append((status, code, str(exc)))
                continue

            if size > max_file:
                issues.append(
                    (
                        "limit_exceeded",
                        "max_file_mb",
                        f"{file_path} is {size} bytes; limit is {max_file} bytes",
                    )
                )
                continue
            if layer_state["bytes"] + size > max_total:
                issues.append(
                    (
                        "limit_exceeded",
                        "max_total_mb",
                        f"{file_path} would exceed layer limit {max_total} bytes",
                    )
                )
                continue

            canonical = canonical_source_key(file_path)
            existing_artifact = deduplicated.get(canonical)
            if existing_artifact is not None:
                keys = existing_artifact.setdefault("meta", {}).setdefault(
                    "artifact_keys", []
                )
                if request.artifact_key not in keys:
                    keys.append(request.artifact_key)
                artifacts.append(existing_artifact)
                stored_paths.append(existing_artifact["stored_path"])
                logical_bytes += int(existing_artifact["size"])
                continue

            try:
                artifact = self._copy_file_streaming(
                    src=file_path,
                    stage_dir=stage_dir,
                    layer=request.layer,
                    artifact_key=request.artifact_key,
                    base_dir=base_dir,
                )
            except Exception as exc:
                status, code = classify_os_error(exc)
                if status in {"locked", "permission_denied"} and "vss" in planned_methods:
                    if "vss" not in attempted_methods:
                        attempted_methods.append("vss")
                    try:
                        shadow = vss_provider.open_volume(file_path.drive)
                        shadow_path = Path(shadow.map_path(str(file_path)))
                        artifact = self._copy_file_streaming(
                            src=file_path,
                            read_source=shadow_path,
                            stage_dir=stage_dir,
                            layer=request.layer,
                            artifact_key=request.artifact_key,
                            base_dir=base_dir,
                            acquisition_method="vss",
                        )
                    except VssSnapshotError as vss_exc:
                        issues.append(("acquisition_failed", vss_exc.code, str(vss_exc)))
                        continue
                    except Exception as vss_exc:
                        vss_status, vss_code = classify_os_error(vss_exc)
                        issues.append((vss_status, vss_code, str(vss_exc)))
                        continue
                else:
                    issues.append((status, code, str(exc)))
                    continue

            deduplicated[canonical] = artifact
            artifacts.append(artifact)
            stored_paths.append(artifact["stored_path"])
            physical_size = int(artifact["size"])
            logical_bytes += physical_size
            added_bytes += physical_size
            layer_state["bytes"] += physical_size
            layer_state["files"] += 1

        if artifacts and not issues:
            status = "collected"
            error_code = None
            message = None
        elif artifacts:
            status = "partially_collected"
            error_code = issues[0][1]
            message = "; ".join(issue[2] for issue in issues[:3])
        else:
            status, error_code, message = issues[0] if issues else (
                "not_present",
                "empty_source",
                "No files were collected",
            )

        outcome_meta = dict(request.meta)
        if any(
            code in {"max_files", "max_file_mb", "max_total_mb"}
            for _, code, _ in issues
        ):
            outcome_meta["truncated"] = True

        return (
            AcquisitionOutcome(
                artifact_key=request.artifact_key,
                layer=request.layer,
                source=source,
                status=status,
                availability=request.availability,
                attempted_methods=attempted_methods,
                artifacts=stored_paths,
                byte_count=logical_bytes,
                error_code=error_code,
                message=message,
                meta=outcome_meta,
            ),
            artifacts,
            added_bytes,
        )

    def _collect_native_request(
        self,
        *,
        request: AcquisitionRequest,
        stage_dir: Path,
        max_file: int,
        max_total: int,
        layer_state: Dict[str, int],
        layer_meta: Dict[str, Any],
    ) -> Tuple[AcquisitionOutcome, List[Dict[str, Any]], int]:
        safe_key = request.artifact_key.replace("\\", "_").replace("/", "_").replace(":", "_")
        with tempfile.TemporaryDirectory(prefix="przma_native_") as temp_name:
            native_dir = Path(temp_name)
            if request.source_type == "registry":
                raw_sources = request.meta.get("native_sources") or []
                native_sources = [str(item) for item in raw_sources] if isinstance(raw_sources, list) else []
                result = self.registry_collector.collect(
                    native_sources=native_sources,
                    destination=native_dir,
                    user_sid=(
                        str(request.meta.get("user_sid"))
                        if request.meta.get("user_sid")
                        else str(layer_meta.get("user_sid"))
                        if layer_meta.get("user_sid")
                        else None
                    ),
                )
            else:
                raw_channels = request.meta.get("channels") or []
                channels = [str(item) for item in raw_channels] if isinstance(raw_channels, list) else []
                result = self.event_log_collector.collect(
                    channels=channels,
                    destination=native_dir,
                )

            artifacts: List[Dict[str, Any]] = []
            stored_paths: List[str] = []
            added_bytes = 0
            failures = list(result.failures)

            for native_file in result.files:
                try:
                    size = native_file.stat().st_size
                    if size > max_file:
                        failures.append(
                            {
                                "source": str(native_file),
                                "error_code": "max_file_mb",
                                "message": f"Native output is {size} bytes; limit is {max_file} bytes",
                            }
                        )
                        continue
                    if layer_state["bytes"] + size > max_total:
                        failures.append(
                            {
                                "source": str(native_file),
                                "error_code": "max_total_mb",
                                "message": f"Native output would exceed layer limit {max_total} bytes",
                            }
                        )
                        continue

                    destination_dir = (
                        stage_dir / "artifacts" / request.layer / safe_key
                    )
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    destination = destination_dir / native_file.name
                    digest = hashlib.sha256()
                    written = 0
                    with native_file.open("rb") as source_handle, destination.open("wb") as destination_handle:
                        while True:
                            chunk = source_handle.read(1024 * 1024)
                            if not chunk:
                                break
                            destination_handle.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)

                    artifact = CollectedArtifact(
                        layer=request.layer,
                        source_path=request.source,
                        stored_path=str(destination),
                        size=written,
                        sha256=digest.hexdigest(),
                        meta={
                            "artifact_key": request.artifact_key,
                            "artifact_keys": [request.artifact_key],
                            "acquisition_method": "native_export",
                            "native_output": native_file.name,
                        },
                    ).to_dict()
                    artifacts.append(artifact)
                    stored_paths.append(str(destination))
                    added_bytes += written
                    layer_state["bytes"] += written
                except Exception as exc:
                    status, code = classify_os_error(exc)
                    failures.append(
                        {
                            "source": str(native_file),
                            "status": status,
                            "error_code": code,
                            "message": str(exc),
                        }
                    )

        if artifacts and not failures:
            status = "collected"
            error_code = None
            message = None
        elif artifacts:
            status = "partially_collected"
            error_code = str(failures[0].get("error_code") or "native_export_partial")
            message = str(failures[0].get("message") or "Native export was partial.")
        else:
            first_failure = failures[0] if failures else {}
            failure_code = str(first_failure.get("error_code") or "native_export_failed")
            status = (
                "permission_denied"
                if failure_code == "access_denied"
                else "limit_exceeded"
                if failure_code in {"max_file_mb", "max_total_mb"}
                else "acquisition_failed"
            )
            error_code = failure_code
            message = str(first_failure.get("message") or "Native export produced no files.")

        return (
            AcquisitionOutcome(
                artifact_key=request.artifact_key,
                layer=request.layer,
                source=request.source,
                status=status,
                availability=request.availability,
                attempted_methods=["native_export"],
                artifacts=stored_paths,
                byte_count=sum(int(artifact["size"]) for artifact in artifacts),
                error_code=error_code,
                message=message,
                meta={**request.meta, "native_failures": failures},
            ),
            artifacts,
            added_bytes,
        )

    def _resolve_user_context(self, policy: SnapshotPolicy) -> UserContext:
        explicit: Dict[str, Any] = {}
        requires_context = False
        user_tokens = {
            "{USERPROFILE}": "USERPROFILE",
            "{APPDATA}": "APPDATA",
            "{LOCALAPPDATA}": "LOCALAPPDATA",
            "{USER_SID}": "user_sid",
            "{CHROME_ROOT}": "CHROME_ROOT",
            "{EDGE_ROOT}": "EDGE_ROOT",
        }

        for layer_name in list(policy.layers or []) or list((policy.layer_policies or {}).keys()):
            layer_policy = (policy.layer_policies or {}).get(layer_name, {})
            if not isinstance(layer_policy, dict):
                continue
            layer_meta = layer_policy.get("meta") or {}
            if isinstance(layer_meta, dict):
                for key in (
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "user_sid",
                    "CHROME_ROOT",
                    "EDGE_ROOT",
                ):
                    if layer_meta.get(key) and key not in explicit:
                        explicit[key] = layer_meta[key]
            text = repr(layer_policy.get("requests") or layer_policy.get("include_paths") or [])
            if any(
                token in text and not explicit.get(explicit_key)
                for token, explicit_key in user_tokens.items()
            ):
                requires_context = True

        configured_profile = str(explicit.get("USERPROFILE") or "").strip() or None
        configured_sid = str(explicit.get("user_sid") or "").strip() or None
        if configured_profile and (
            configured_sid or "{USER_SID}" not in repr(policy.layer_policies)
        ):
            return UserContext(
                user_sid=configured_sid,
                user_profile=configured_profile,
                source="config",
            )
        if not requires_context:
            return UserContext(source="not_required")

        detected = self.user_context_resolver.resolve()
        if configured_profile:
            detected.user_profile = configured_profile
            detected.source = "config_and_active_session"
        if configured_sid:
            detected.user_sid = configured_sid
            detected.source = "config_and_active_session"
        return detected

    def _native_fallback_paths(
        self,
        request: AcquisitionRequest,
        outcome: AcquisitionOutcome,
        layer_meta: Dict[str, Any],
    ) -> List[str]:
        failures = outcome.meta.get("native_failures") or []
        paths = request.meta.get("paths") or []
        source_key = "native_sources" if request.source_type == "registry" else "channels"
        native_sources = request.meta.get(source_key) or []
        if not (
            isinstance(failures, list)
            and isinstance(paths, list)
            and isinstance(native_sources, list)
            and len(paths) == len(native_sources)
        ):
            return []

        user_sid = str(
            request.meta.get("user_sid")
            or layer_meta.get("user_sid")
            or ""
        )
        index_by_source = {}
        for index, source in enumerate(native_sources):
            normalized = str(source)
            if user_sid:
                normalized = normalized.replace("{USER_SID}", user_sid)
            index_by_source[normalized] = index
        selected: List[str] = []
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            source = str(failure.get("source") or "")
            index = index_by_source.get(source)
            if index is None:
                continue
            candidate = str(paths[index])
            if candidate not in selected:
                selected.append(candidate)
        return selected

    def _copy_file_streaming(
        self,
        *,
        src: Path,
        stage_dir: Path,
        layer: str,
        artifact_key: str,
        base_dir: Optional[Path] = None,
        read_source: Optional[Path] = None,
        acquisition_method: str = "direct",
    ) -> Dict[str, Any]:
        input_path = read_source or src
        size = input_path.stat().st_size
        safe_key = artifact_key.replace("\\", "_").replace("/", "_").replace(":", "_")
        layer_dir = stage_dir / "artifacts" / layer / safe_key
        layer_dir.mkdir(parents=True, exist_ok=True)

        dst, path_meta = _bounded_destination(
            layer_dir=layer_dir,
            src=src,
            base_dir=base_dir,
        )
        dst.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        written = 0
        try:
            with input_path.open("rb") as source_handle, dst.open("wb") as destination_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    destination_handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
        except Exception:
            if dst.exists():
                dst.unlink()
            fallback = _read_file_bytes(input_path)
            if fallback is None:
                raise
            with dst.open("wb") as destination_handle:
                destination_handle.write(fallback)
            digest = hashlib.sha256(fallback)
            written = len(fallback)

        return CollectedArtifact(
            layer=layer,
            source_path=str(src),
            stored_path=str(dst),
            size=written if written != size else size,
            sha256=digest.hexdigest(),
            meta={
                "artifact_key": artifact_key,
                "artifact_keys": [artifact_key],
                "acquisition_method": acquisition_method,
                **path_meta,
            },
        ).to_dict()

    def _make_zip(self, folder: Path, zip_path: Path) -> None:
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in folder.rglob("*"):
                if p.is_file():
                    # Use forward slashes so cache dump prefix matching works cross-platform
                    arcname = p.relative_to(folder).as_posix()
                    zf.write(p, arcname=arcname)
