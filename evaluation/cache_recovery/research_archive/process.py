from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import requests

from .server import ResearchArchiveServer


ROOT = Path(__file__).resolve().parents[3]
CONTROL_TOKEN_ENV = "PRZMA_ARCHIVE_CONTROL_TOKEN"


class ResearchArchiveControlClient:
    def __init__(self, base_url: str, token: str, timeout_sec: int = 10) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout_sec = int(timeout_sec)

    def _headers(self) -> Dict[str, str]:
        return {"X-Archive-Control-Token": self.token}

    def wait_until_healthy(self, timeout_sec: int = 30) -> None:
        deadline = time.monotonic() + max(1, int(timeout_sec))
        last_error = "control server did not respond"
        while time.monotonic() < deadline:
            try:
                response = requests.get(
                    f"{self.base_url}/health",
                    headers=self._headers(),
                    timeout=2,
                )
                response.raise_for_status()
                if response.json().get("ok") is True:
                    return
                last_error = f"unexpected response: {response.text[:160]}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(0.25)
        raise RuntimeError(f"Research Archive control health timeout: {last_error}")

    def register_run(self, run_id: str) -> Dict[str, object]:
        response = requests.post(
            f"{self.base_url}/runs",
            headers=self._headers(),
            json={"run_id": run_id},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Research Archive registration response must be an object")
        return value

    def status(self, run_id: str) -> Dict[str, object]:
        response = requests.get(
            f"{self.base_url}/runs/{run_id}",
            headers=self._headers(),
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Research Archive status response must be an object")
        return value

    def shutdown(self) -> None:
        response = requests.post(
            f"{self.base_url}/shutdown",
            headers=self._headers(),
            json={},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()


class ArchiveControlServer:
    """Host-loopback control plane, physically separate from the public site."""

    def __init__(
        self,
        site: ResearchArchiveServer,
        *,
        token: str,
        port: int,
    ) -> None:
        if not token:
            raise ValueError("control token is required")
        self.site = site
        self.token = token
        self._requested_port = int(port)
        self.shutdown_requested = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._requested_port
        return int(self._httpd.server_address[1])

    def _status(self, run_id: str) -> Optional[Dict[str, object]]:
        run = self.site._get_run(run_id)
        if run is None:
            return None
        with run.lock:
            return {
                "run_id": run.run_id,
                "request_count": len(run.requests),
                "effect_count": len(run.effects),
                "requested_resource_ids": sorted(run.requested_ids),
            }

    def start(self) -> "ArchiveControlServer":
        if self._httpd is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArchiveControl/1.0"

            def log_message(self, format: str, *args) -> None:
                return

            def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
                body = json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if self.headers.get("X-Archive-Control-Token", "") == owner.token:
                    return True
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return False

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if not self._authorized():
                    return
                if parsed.path.startswith("/runs/"):
                    run_id = parsed.path.removeprefix("/runs/")
                    status = owner._status(run_id)
                    if status is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_run"})
                    else:
                        self._send_json(HTTPStatus.OK, status)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                if not self._authorized():
                    return
                if parsed.path == "/runs":
                    try:
                        length = min(int(self.headers.get("Content-Length") or 0), 4096)
                        payload = json.loads(self.rfile.read(length).decode("utf-8"))
                        run_id = str(payload.get("run_id") or "").strip()
                        owner.site.register_run(run_id)
                    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_run", "detail": str(exc)[:160]},
                        )
                        return
                    self._send_json(HTTPStatus.CREATED, {"run_id": run_id})
                    return
                if parsed.path == "/shutdown":
                    owner.shutdown_requested.set()
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._requested_port), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="research-archive-control",
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


class ResearchArchiveProcess:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        control_port: int,
        log_dir: Path,
        root: Path = ROOT,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.control_port = int(control_port)
        self.log_dir = Path(log_dir)
        self.root = Path(root)
        self.control_token = secrets.token_urlsafe(32)
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.client = ResearchArchiveControlClient(
            f"http://127.0.0.1:{self.control_port}",
            self.control_token,
        )

    @staticmethod
    def _powershell_literal(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _command(self) -> list[str]:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        module_args = [
            sys.executable,
            "-m",
            "evaluation.cache_recovery.research_archive.process",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--control-port",
            str(self.control_port),
            "--log-dir",
            str(self.log_dir),
        ]
        script = "& " + " ".join(self._powershell_literal(arg) for arg in module_args)
        script += "; exit $LASTEXITCODE"
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]

    def start(self) -> ResearchArchiveControlClient:
        if self.process is not None and self.process.poll() is None:
            return self.client
        environment = os.environ.copy()
        environment[CONTROL_TOKEN_ENV] = self.control_token
        self.process = subprocess.Popen(
            self._command(),
            cwd=self.root,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        try:
            self.client.wait_until_healthy(timeout_sec=30)
        except Exception:
            self.stop()
            raise
        return self.client

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.client.shutdown()
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.process = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the Research Archive evaluation server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--control-port", type=int, default=8766)
    parser.add_argument("--log-dir", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get(CONTROL_TOKEN_ENV, "")
    if not token:
        raise RuntimeError(f"Missing environment variable: {CONTROL_TOKEN_ENV}")
    site = ResearchArchiveServer(
        host=args.host,
        port=args.port,
        log_dir=args.log_dir,
    ).start()
    control = ArchiveControlServer(site, token=token, port=args.control_port).start()
    print("Research Archive evaluation server is running", flush=True)
    print(f"Public listener: http://{args.host}:{site.port}", flush=True)
    print(f"Host control listener: http://127.0.0.1:{control.port}", flush=True)
    print("Waiting for the experiment launcher. Press Ctrl+C to stop.", flush=True)
    try:
        while not control.shutdown_requested.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("Stopping Research Archive server...", flush=True)
    finally:
        control.stop()
        site.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
