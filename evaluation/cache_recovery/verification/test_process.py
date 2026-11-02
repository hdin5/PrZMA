from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from evaluation.cache_recovery.research_archive.process import (
    ArchiveControlServer,
    ResearchArchiveControlClient,
    ResearchArchiveProcess,
)
from evaluation.cache_recovery.research_archive.server import ResearchArchiveServer


class ResearchArchiveProcessTests(unittest.TestCase):
    def test_loopback_control_registers_run_without_public_control_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            site = ResearchArchiveServer(
                host="127.0.0.1",
                port=0,
                log_dir=Path(temp_dir),
            ).start()
            control = ArchiveControlServer(site, token="control-secret", port=0).start()
            client = ResearchArchiveControlClient(
                f"http://127.0.0.1:{control.port}",
                "control-secret",
            )
            try:
                registered = client.register_run("dev01")
                status = client.status("dev01")
                public = requests.post(
                    f"{site.base_url}/control/register",
                    json={"run_id": "bad"},
                    timeout=2,
                )
            finally:
                control.stop()
                site.stop()

        self.assertEqual(registered, {"run_id": "dev01"})
        self.assertEqual(status["run_id"], "dev01")
        self.assertEqual(status["request_count"], 0)
        self.assertEqual(public.status_code, 404)
        self.assertEqual(public.headers["Cache-Control"], "no-store")

    def test_control_requires_token_and_uses_no_store(self) -> None:
        site = ResearchArchiveServer(host="127.0.0.1", port=0).start()
        control = ArchiveControlServer(site, token="control-secret", port=0).start()
        try:
            response = requests.post(
                f"http://127.0.0.1:{control.port}/runs",
                json={"run_id": "dev01"},
                timeout=2,
            )
        finally:
            control.stop()
            site.stop()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_visible_process_uses_new_console_and_keeps_token_out_of_arguments(self) -> None:
        process_handle = MagicMock()
        process_handle.poll.return_value = None
        runtime = ResearchArchiveProcess(
            host="0.0.0.0",
            port=8765,
            control_port=8766,
            log_dir=Path("evaluation/results"),
        )

        with patch(
            "evaluation.cache_recovery.research_archive.process.subprocess.Popen",
            return_value=process_handle,
        ) as popen:
            with patch.object(runtime.client, "wait_until_healthy"):
                runtime.start()

        command = popen.call_args.args[0]
        self.assertNotIn(runtime.control_token, " ".join(command))
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        self.assertEqual(
            popen.call_args.kwargs["env"]["PRZMA_ARCHIVE_CONTROL_TOKEN"],
            runtime.control_token,
        )
        self.assertIn("evaluation.cache_recovery.research_archive.process", " ".join(command))


if __name__ == "__main__":
    unittest.main()
