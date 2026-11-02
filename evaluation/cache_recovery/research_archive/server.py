from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .definition import SiteDefinition
from .manifest import ResourceSpec, build_resource_spec
from .sites.research_archive import research_archive_definition


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Ports blocked by Chromium even for loopback navigation. Dynamic test servers
# must not expose an otherwise valid evaluation site on one of these ports.
_CHROMIUM_RESTRICTED_PORTS = frozenset(
    {
        1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
        69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115,
        117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512,
        513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587,
        601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045,
        5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
    }
)
_DYNAMIC_PORT_ATTEMPTS = 32


def _bind_browser_safe_http_server(
    host: str,
    requested_port: int,
    handler: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    requested_port = int(requested_port)
    if requested_port:
        if requested_port in _CHROMIUM_RESTRICTED_PORTS:
            raise ValueError(f"Chromium blocks navigation to port {requested_port}")
        return ThreadingHTTPServer((host, requested_port), handler)

    for _ in range(_DYNAMIC_PORT_ATTEMPTS):
        httpd = ThreadingHTTPServer((host, 0), handler)
        if int(httpd.server_address[1]) not in _CHROMIUM_RESTRICTED_PORTS:
            return httpd
        httpd.server_close()
    raise OSError("Could not allocate a Chromium-safe dynamic port")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_traversal(path: str) -> bool:
    decoded = path
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    normalized = decoded.replace("\\", "/")
    return any(segment == ".." for segment in normalized.split("/"))


@dataclass
class RegisteredArchiveRun:
    run_id: str
    requested_ids: set[str] = field(default_factory=set)
    requests: list[Dict[str, object]] = field(default_factory=list)
    effects: list[Dict[str, object]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _next_request_sequence: int = field(default=1, repr=False)

    def record_response(
        self,
        spec: ResourceSpec,
        *,
        request_url: str,
        method: str,
        status: int,
        request_headers: Dict[str, str],
        persist: Optional[Callable[[Dict[str, object]], None]] = None,
    ) -> Dict[str, object]:
        with self.lock:
            event: Dict[str, object] = {
                "ts": _now_iso(),
                "run_id": self.run_id,
                "request_sequence": self._next_request_sequence,
                "resource_id": spec.resource_id,
                "variant_id": spec.variant_id,
                "classification": spec.classification,
                "trigger": spec.trigger,
                "request_order": spec.request_order,
                "request_url": request_url,
                "parameters": dict(spec.parameter_variant),
                "method": method,
                "status": int(status),
                "media_type": spec.media_type,
                "content_encoding": spec.content_encoding,
                "decoded_size": spec.decoded_size,
                "encoded_size": spec.encoded_size,
                "decoded_sha256": spec.decoded_sha256,
                "encoded_sha256": spec.encoded_sha256,
                "cache_headers": dict(spec.cache_headers),
                "user_agent": request_headers.get("User-Agent", ""),
                "referer": request_headers.get("Referer", ""),
            }
            self._next_request_sequence += 1
            self.requested_ids.add(spec.resource_id)
            self.requests.append(event)
            if persist is not None:
                persist(event)
            return event

    def record_effect(
        self,
        effect: str,
        parameters: Dict[str, str],
        *,
        persist: Optional[Callable[[Dict[str, object]], None]] = None,
    ) -> Dict[str, object]:
        with self.lock:
            event: Dict[str, object] = {
                "ts": _now_iso(),
                "run_id": self.run_id,
                "effect": effect,
                "parameters": dict(sorted(parameters.items())),
            }
            self.effects.append(event)
            if persist is not None:
                persist(event)
            return event


class ResearchArchiveServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        log_dir: Optional[Path] = None,
        definition: Optional[SiteDefinition] = None,
    ) -> None:
        self.host = host
        self._requested_port = int(port)
        self.log_dir = Path(log_dir) if log_dir else None
        self.definition = definition or research_archive_definition()
        self.runs: Dict[str, RegisteredArchiveRun] = {}
        self._runs_lock = threading.RLock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._log_lock = threading.RLock()

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._requested_port
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def register_run(self, run_id: str) -> RegisteredArchiveRun:
        normalized = str(run_id).strip()
        if not RUN_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "run_id must be 1-64 characters using letters, digits, dot, underscore, or hyphen"
            )
        with self._runs_lock:
            if normalized in self.runs:
                raise ValueError(f"Run is already registered: {normalized}")
            run = RegisteredArchiveRun(normalized)
            self.runs[normalized] = run
            return run

    def _get_run(self, run_id: str) -> Optional[RegisteredArchiveRun]:
        with self._runs_lock:
            return self.runs.get(run_id)

    def _append_jsonl(
        self,
        run: RegisteredArchiveRun,
        filename: str,
        event: Dict[str, object],
    ) -> None:
        if self.log_dir is None:
            return
        destination = self.log_dir / f"run_{run.run_id}"
        with self._log_lock:
            destination.mkdir(parents=True, exist_ok=True)
            with (destination / filename).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def start(self) -> "ResearchArchiveServer":
        if self._httpd is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "EvaluationSite/2.0"

            def log_message(self, format: str, *args) -> None:
                return

            def _send(
                self,
                status: int,
                body: bytes,
                media_type: str,
                *,
                headers: Optional[Dict[str, str]] = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", media_type)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                if "Content-Length" not in (headers or {}):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)

            def _send_control_json(self, status: int, payload: Dict[str, object]) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._send(
                    status,
                    body,
                    "application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                )

            def _run_from_cookie(self) -> Optional[RegisteredArchiveRun]:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get("ra_run")
                return owner._get_run(morsel.value) if morsel else None

            def _request_url(self) -> str:
                host = self.headers.get("Host") or f"{owner.host}:{owner.port}"
                return f"http://{host}{self.path}"

            def _serve_definition_resource(
                self,
                run: RegisteredArchiveRun,
                path: str,
                query: Dict[str, list[str]],
            ) -> None:
                resource = owner.definition.resource_for(self.command, path)
                if resource is None:
                    self._send_control_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    parameters = resource.normalize_parameters(query)
                    variant = resource.variant_for(parameters)
                    spec = build_resource_spec(owner.definition, resource, variant)
                    decoded = resource.decoded_body(owner.definition.root, parameters)
                    encoded = resource.encoded_body(decoded)
                except ValueError as error:
                    self._send_control_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_parameters", "detail": str(error)},
                    )
                    return
                except FileNotFoundError:
                    self._send_control_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return

                cache_headers = dict(spec.cache_headers)
                if_none_match = self.headers.get("If-None-Match")
                if_modified_since = self.headers.get("If-Modified-Since")
                not_modified = (
                    if_none_match == spec.etag
                    or (if_none_match is None and if_modified_since == spec.last_modified)
                )
                response_status = HTTPStatus.NOT_MODIFIED if not_modified else resource.status
                response_body = b"" if not_modified else encoded
                response_headers = {
                    **cache_headers,
                    "Content-Length": str(len(response_body)),
                }

                run.record_response(
                    spec,
                    request_url=self._request_url(),
                    method=self.command,
                    status=response_status,
                    request_headers={name: value for name, value in self.headers.items()},
                    persist=lambda event: owner._append_jsonl(
                        run,
                        "archive_requests.jsonl",
                        event,
                    ),
                )
                self._send(
                    response_status,
                    response_body,
                    spec.media_type,
                    headers=response_headers,
                )

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if _has_traversal(parsed.path):
                    self._send_control_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_path"},
                    )
                    return
                if parsed.path == "/health":
                    self._send_control_json(
                        HTTPStatus.OK,
                        {"ok": True, "service": owner.definition.site_id},
                    )
                    return
                if parsed.path == "/start":
                    run_id = (parse_qs(parsed.query).get("run_id") or [""])[0]
                    if owner._get_run(run_id) is None:
                        self._send_control_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "unknown_run", "run_id": run_id},
                        )
                        return
                    self._send(
                        HTTPStatus.FOUND,
                        b"",
                        "text/plain; charset=utf-8",
                        headers={
                            "Cache-Control": "no-store",
                            "Location": owner.definition.entry_path,
                            "Set-Cookie": f"ra_run={run_id}; Path=/; HttpOnly; SameSite=Lax",
                        },
                    )
                    return

                run = self._run_from_cookie()
                if run is None:
                    self._send_control_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "run_session_required"},
                    )
                    return
                self._serve_definition_resource(run, parsed.path, parse_qs(parsed.query))

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/events":
                    self._send_control_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                run = self._run_from_cookie()
                if run is None:
                    self._send_control_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "run_session_required"},
                    )
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 8192)
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send_control_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                    return
                effect = str(payload.get("effect") or "").strip()[:80]
                raw_parameters = payload.get("parameters") or {}
                if not effect or not isinstance(raw_parameters, dict):
                    self._send_control_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_event"})
                    return
                parameters = {
                    str(key)[:80]: str(value)[:240]
                    for key, value in raw_parameters.items()
                }
                run.record_effect(
                    effect,
                    parameters,
                    persist=lambda event: owner._append_jsonl(
                        run,
                        "archive_effects.jsonl",
                        event,
                    ),
                )
                self._send_control_json(HTTPStatus.OK, {"ok": True})

        self._httpd = _bind_browser_safe_http_server(
            self.host,
            self._requested_port,
            Handler,
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"{self.definition.site_id}-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None


@contextmanager
def running_research_archive_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    log_dir: Optional[Path] = None,
    definition: Optional[SiteDefinition] = None,
) -> Iterator[ResearchArchiveServer]:
    server = ResearchArchiveServer(
        host=host,
        port=port,
        log_dir=log_dir,
        definition=definition,
    ).start()
    try:
        yield server
    finally:
        server.stop()
