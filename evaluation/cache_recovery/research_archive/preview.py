from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional, Sequence

from .server import ResearchArchiveServer


def create_preview(
    *,
    host: str,
    port: int,
    run_id: str,
    log_dir: Optional[Path] = None,
) -> tuple[ResearchArchiveServer, str]:
    server = ResearchArchiveServer(host=host, port=port, log_dir=log_dir).start()
    server.register_run(run_id)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{server.port}/start?run_id={run_id}"
    return server, url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the Research Archive preview server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", default="preview")
    parser.add_argument("--log-dir", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    server, url = create_preview(
        host=args.host,
        port=args.port,
        run_id=args.run_id,
        log_dir=args.log_dir,
    )
    print("Research Archive preview is running", flush=True)
    print(f"Open: {url}", flush=True)
    print("Press Ctrl+C to stop the server.", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping preview server...", flush=True)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
