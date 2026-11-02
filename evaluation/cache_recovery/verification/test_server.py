from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)


class ResearchArchiveServerTests(unittest.TestCase):
    def _server_api(self):
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        return running_research_archive_server

    @staticmethod
    def _session(server, run_id: str):
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        response = opener.open(f"{server.base_url}/start?run_id={run_id}", timeout=3)
        return opener, response

    def test_dynamic_port_retries_chromium_restricted_assignment(self) -> None:
        from evaluation.cache_recovery.research_archive.server import (
            _bind_browser_safe_http_server,
        )

        restricted = MagicMock(server_address=("127.0.0.1", 6669))
        safe = MagicMock(server_address=("127.0.0.1", 8765))
        with patch(
            "evaluation.cache_recovery.research_archive.server.ThreadingHTTPServer",
            side_effect=[restricted, safe],
        ) as factory:
            selected = _bind_browser_safe_http_server(
                "127.0.0.1",
                0,
                MagicMock(),
            )

        self.assertIs(selected, safe)
        self.assertEqual(factory.call_count, 2)
        restricted.server_close.assert_called_once_with()

    def test_start_sets_run_cookie_and_home_does_not_auto_run_interactions(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")

            _opener, response = self._session(server, "pilot01")
            page = response.read().decode("utf-8")

            self.assertIn("Research Archive", page)
            self.assertEqual(
                server.runs["pilot01"].requested_ids,
                {"passive.home_html"},
            )

    def test_unknown_run_cannot_start_session(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{server.base_url}/start?run_id=missing", timeout=3)
            self.assertEqual(raised.exception.code, 404)

    def test_run_id_rejects_path_and_header_unsafe_values(self) -> None:
        unsafe = ("../escape", "nested/run", "cookie;value", "line\r\nbreak", "", " " * 3)
        with self._server_api()(host="127.0.0.1", port=0) as server:
            for run_id in unsafe:
                with self.subTest(run_id=repr(run_id)), self.assertRaises(ValueError):
                    server.register_run(run_id)
            accepted = server.register_run("pilot_20260718-01.test")
            self.assertEqual(accepted.run_id, "pilot_20260718-01.test")

    def test_preview_factory_registers_run_and_returns_local_entry_url(self) -> None:
        from evaluation.cache_recovery.research_archive.preview import create_preview

        server, url = create_preview(host="127.0.0.1", port=0, run_id="preview")
        try:
            self.assertIn("preview", server.runs)
            self.assertEqual(url, f"{server.base_url}/start?run_id=preview")
            self.assertGreater(server.port, 0)
        finally:
            server.stop()

    def test_ground_truth_and_control_paths_are_not_public(self) -> None:
        blocked = (
            "/manifest",
            "/resource_manifest.json",
            "/behavior_manifest.json",
            "/oracle",
            "/control",
            "/runs/pilot01",
        )
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()
            for path in blocked:
                with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                    opener.open(f"{server.base_url}{path}", timeout=3)
                self.assertEqual(raised.exception.code, 404)

    def test_same_server_serves_a_second_data_only_site_definition(self) -> None:
        from evaluation.cache_recovery.research_archive.definition import (
            BehaviorDefinition,
            ParameterVariant,
            ResourceDefinition,
            SiteDefinition,
        )

        holdout = SiteDefinition(
            site_id="holdout-library",
            service_name="Holdout Library",
            version="2026-07-18",
            entry_path="/welcome",
            resources=(
                ResourceDefinition(
                    resource_id="shell.document",
                    classification="passive",
                    journey="entry",
                    trigger="entry_navigation",
                    request_order=1,
                    path="/welcome",
                    media_type="text/html; charset=utf-8",
                    body=b"<!doctype html><title>Holdout Library</title>",
                ),
                ResourceDefinition(
                    resource_id="browse.record",
                    classification="interactive",
                    journey="browse",
                    trigger="record_open",
                    request_order=1,
                    path="/record",
                    media_type="application/json; charset=utf-8",
                    body=b'{"record":"independent"}',
                    variants=(ParameterVariant("default"),),
                ),
            ),
            behaviors=(
                BehaviorDefinition(
                    behavior_id="browse.record_open",
                    journey="browse",
                    effect="Open one record",
                    evidence_resource_ids=("browse.record",),
                ),
            ),
        )

        with self._server_api()(
            host="127.0.0.1",
            port=0,
            definition=holdout,
        ) as server:
            server.register_run("holdout01")
            opener, response = self._session(server, "holdout01")
            page = response.read().decode("utf-8")
            record = json.load(opener.open(f"{server.base_url}/record", timeout=3))

            self.assertIn("Holdout Library", page)
            self.assertEqual(record, {"record": "independent"})
            self.assertEqual(
                server.runs["holdout01"].requested_ids,
                {"shell.document", "browse.record"},
            )

    def test_concurrent_requests_have_unique_monotonic_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._server_api()(
                host="127.0.0.1",
                port=0,
                log_dir=Path(tmp),
            ) as server:
                server.register_run("parallel")

                def request_once(_index: int) -> None:
                    opener, home = self._session(server, "parallel")
                    home.read()
                    opener.open(f"{server.base_url}/static/css/base.css", timeout=3).read()

                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(request_once, range(12)))

                events = list(server.runs["parallel"].requests)

            sequences = [int(event["request_sequence"]) for event in events]
            self.assertEqual(len(events), 24)
            self.assertEqual(sorted(sequences), list(range(1, 25)))
            ledger = Path(tmp) / "run_parallel" / "archive_requests.jsonl"
            persisted = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(persisted), 24)
            self.assertEqual(
                sorted(int(event["request_sequence"]) for event in persisted),
                list(range(1, 25)),
            )

    def test_concurrent_duplicate_run_registration_creates_one_run(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            def register(_index: int) -> str:
                try:
                    server.register_run("same-run")
                    return "created"
                except ValueError:
                    return "duplicate"

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(register, range(20)))

            self.assertEqual(results.count("created"), 1)
            self.assertEqual(results.count("duplicate"), 19)
            self.assertEqual(list(server.runs), ["same-run"])

    def test_path_traversal_is_rejected(self) -> None:
        attempts = (
            "/static/../templates/home.html",
            "/static/%2e%2e/templates/home.html",
            "/static/%252e%252e/templates/home.html",
            "/static/%2e%2e%5ctemplates%5chome.html",
            "/documents/..%2ftemplates/home.html",
        )
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()
            for path in attempts:
                with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                    opener.open(f"{server.base_url}{path}", timeout=3)
                self.assertIn(raised.exception.code, {400, 404})

    def test_health_is_no_store(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            with urlopen(f"{server.base_url}/health", timeout=3) as response:
                payload = json.load(response)
                cache_control = response.headers.get("Cache-Control")

            self.assertEqual(payload, {"ok": True, "service": "research-archive"})
            self.assertEqual(cache_control, "no-store")

    def test_start_redirect_is_no_store(self) -> None:
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener = build_opener(NoRedirect())
            with self.assertRaises(HTTPError) as raised:
                opener.open(f"{server.base_url}/start?run_id=pilot01", timeout=3)
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(raised.exception.headers.get("Cache-Control"), "no-store")

    def test_every_canonical_response_matches_the_generated_catalog(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("catalog")
            opener, response = self._session(server, "catalog")
            response.read()

            for spec in resource_specs(server.definition):
                with self.subTest(resource=spec.resource_id, variant=spec.variant_id):
                    with opener.open(f"{server.base_url}{spec.url}", timeout=5) as actual:
                        body = actual.read()
                        headers = actual.headers
                        self.assertEqual(actual.status, spec.status)
                    expected_headers = dict(spec.cache_headers)
                    self.assertEqual(headers.get_content_type(), spec.media_type.split(";", 1)[0])
                    self.assertEqual(headers.get("Content-Encoding"), spec.content_encoding)
                    self.assertEqual(headers.get("Cache-Control"), expected_headers["Cache-Control"])
                    self.assertEqual(headers.get("ETag"), expected_headers["ETag"])
                    self.assertEqual(headers.get("Last-Modified"), expected_headers["Last-Modified"])
                    self.assertEqual(headers.get("Vary"), expected_headers["Vary"])
                    self.assertEqual(int(headers.get("Content-Length")), spec.encoded_size)
                    self.assertEqual(len(body), spec.encoded_size)
                    self.assertEqual(hashlib.sha256(body).hexdigest(), spec.encoded_sha256)
                    if spec.content_encoding == "identity":
                        self.assertEqual(len(body), spec.decoded_size)
                        self.assertEqual(hashlib.sha256(body).hexdigest(), spec.decoded_sha256)

    def test_etag_conditional_request_records_304_without_a_body(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        target = next(item for item in resource_specs() if item.resource_id == "passive.base_css")
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("conditional")
            opener, response = self._session(server, "conditional")
            response.read()
            request = Request(
                f"{server.base_url}{target.url}",
                headers={"If-None-Match": target.etag},
            )
            with self.assertRaises(HTTPError) as raised:
                opener.open(request, timeout=3)

            self.assertEqual(raised.exception.code, 304)
            self.assertEqual(raised.exception.read(), b"")
            event = server.runs["conditional"].requests[-1]
            self.assertEqual(event["status"], 304)
            self.assertEqual(event["encoded_sha256"], target.encoded_sha256)

    def test_sessions_keep_request_ledgers_separate(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("first")
            server.register_run("second")
            first, first_home = self._session(server, "first")
            second, second_home = self._session(server, "second")
            first_home.read()
            second_home.read()

            first.open(f"{server.base_url}/static/css/base.css", timeout=3).read()

            self.assertEqual(
                server.runs["first"].requested_ids,
                {"passive.home_html", "passive.base_css"},
            )
            self.assertEqual(server.runs["second"].requested_ids, {"passive.home_html"})

    def test_request_ledger_persists_exact_response_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._server_api()(
                host="127.0.0.1",
                port=0,
                log_dir=Path(tmp),
            ) as server:
                server.register_run("pilot01")
                opener, response = self._session(server, "pilot01")
                response.read()
                opener.open(f"{server.base_url}/static/css/base.css", timeout=3).read()

            ledger = Path(tmp) / "run_pilot01" / "archive_requests.jsonl"
            events = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["resource_id"] for event in events],
                ["passive.home_html", "passive.base_css"],
            )
            for event in events:
                self.assertEqual(len(event["decoded_sha256"]), 64)
                self.assertEqual(len(event["encoded_sha256"]), 64)
                self.assertGreater(event["decoded_size"], 0)
                self.assertEqual(event["decoded_size"], event["encoded_size"])

    def test_public_routes_have_unique_titles_and_neutral_copy(self) -> None:
        routes = {
            "/": "Research Archive",
            "/collections": "Collections · Research Archive",
            "/collections/environmental-signals": "Environmental Signals · Research Archive",
            "/search?query=coastal": "Search · Research Archive",
            "/reports/web-evidence": "Preserving Web Evidence · Research Archive",
            "/datasets/regional-observations": "Regional Observation Series · Research Archive",
            "/map": "Observation Map · Research Archive",
        }
        forbidden_copy = ("PrZMA", "cache object", "oracle", "coverage", "expected action")

        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()

            for route, title in routes.items():
                page = opener.open(f"{server.base_url}{route}", timeout=3).read().decode("utf-8")
                self.assertIn(f"<title>{title}</title>", page, route)
                self.assertIn("Research Archive", page, route)
                self.assertIn("<main", page, route)
                self.assertIn('aria-label="Primary navigation"', page, route)
                for phrase in forbidden_copy:
                    self.assertNotIn(phrase, page, route)

            with self.assertRaises(HTTPError) as raised:
                opener.open(f"{server.base_url}/atlas", timeout=3)
            self.assertEqual(raised.exception.code, 404)

    def test_each_route_loads_only_its_feature_assets(self) -> None:
        expected = {
            "/": ("/static/css/base.css", "/static/js/base.js"),
            "/collections": ("/static/css/collections.css",),
            "/collections/environmental-signals": (
                "/static/css/collections.css",
                "/static/js/collections.js",
            ),
            "/search?query=coastal": (
                "/static/css/search.css",
                "/static/js/search.js",
            ),
            "/reports/web-evidence": (
                "/static/css/report.css",
                "/static/js/report.js",
            ),
            "/datasets/regional-observations": (
                "/static/css/dataset.css",
                "/static/js/dataset.js",
            ),
            "/map": ("/static/css/map.css",),
        }
        all_feature_assets = {
            asset
            for assets in expected.values()
            for asset in assets
            if "base." not in asset
        }

        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()

            for route, required_assets in expected.items():
                page = opener.open(f"{server.base_url}{route}", timeout=3).read().decode("utf-8")
                for asset in required_assets:
                    self.assertIn(asset, page, route)
                unexpected = all_feature_assets.difference(required_assets)
                for asset in unexpected:
                    self.assertNotIn(asset, page, route)

    def test_interactive_documents_do_not_embed_target_requests_eagerly(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            home = response.read().decode("utf-8")
            collection = opener.open(
                f"{server.base_url}/collections/environmental-signals",
                timeout=3,
            ).read().decode("utf-8")
            report = opener.open(
                f"{server.base_url}/reports/web-evidence",
                timeout=3,
            ).read().decode("utf-8")

            self.assertNotIn("/api/", home)
            self.assertNotIn("/embed/map", home)
            self.assertIn('data-src="/static/images/coastal-observation.webp"', collection)
            self.assertNotRegex(
                collection,
                r'(?<!data-)src="/static/images/coastal-observation\.webp"',
            )
            self.assertNotIn("<iframe", collection)
            self.assertNotIn("/documents/web-evidence.pdf", report)
            self.assertNotIn("<iframe", report)

    def test_dynamic_search_preserves_valid_agent_parameters(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()
            url = (
                f"{server.base_url}/api/search?query=coastal&topic=environment"
                "&year=2026&format=report&sort=newest"
            )

            with opener.open(url, timeout=3) as api_response:
                payload = json.load(api_response)
                cache_control = api_response.headers.get("Cache-Control")

            self.assertEqual(
                payload["parameters"],
                {
                    "query": "coastal",
                    "topic": "environment",
                    "year": "2026",
                    "format": "report",
                    "sort": "newest",
                },
            )
            self.assertEqual(cache_control, "public, max-age=3600")
            self.assertIn("inquiry.json.01", server.runs["pilot01"].requested_ids)

    def test_dynamic_endpoints_reject_invalid_parameters_without_substitution(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()

            with self.assertRaises(HTTPError) as raised:
                opener.open(
                    f"{server.base_url}/api/search?query=coastal&topic=unknown",
                    timeout=3,
                )
            self.assertEqual(raised.exception.code, 400)
            self.assertNotIn("inquiry.json.01", server.runs["pilot01"].requested_ids)

    def test_dataset_preview_and_map_data_are_cacheable_and_logged(self) -> None:
        with self._server_api()(host="127.0.0.1", port=0) as server:
            server.register_run("pilot01")
            opener, response = self._session(server, "pilot01")
            response.read()

            preview = json.load(
                opener.open(
                    f"{server.base_url}/api/datasets/regional-observations"
                    "?region=estuary&measure=salinity",
                    timeout=3,
                )
            )
            map_data = json.load(
                opener.open(f"{server.base_url}/api/map/observations", timeout=3)
            )

            self.assertEqual(preview["parameters"], {"region": "estuary", "measure": "salinity"})
            self.assertGreater(len(preview["rows"]), 0)
            self.assertGreater(len(map_data["sites"]), 0)
            self.assertTrue(
                {"retrieve.json.01", "discover.json.02"}
                <= server.runs["pilot01"].requested_ids
            )

    def test_effect_endpoint_is_no_store_and_persists_normalized_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._server_api()(
                host="127.0.0.1",
                port=0,
                log_dir=Path(tmp),
            ) as server:
                server.register_run("pilot01")
                opener, response = self._session(server, "pilot01")
                response.read()
                body = json.dumps(
                    {
                        "effect": "filter_applied",
                        "parameters": {"topic": "environment", "year": "2026"},
                    }
                ).encode("utf-8")
                request = Request(
                    f"{server.base_url}/events",
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with opener.open(request, timeout=3) as event_response:
                    self.assertEqual(event_response.headers.get("Cache-Control"), "no-store")

            ledger = Path(tmp) / "run_pilot01" / "archive_effects.jsonl"
            event = json.loads(ledger.read_text(encoding="utf-8").strip())
            self.assertEqual(event["effect"], "filter_applied")
            self.assertEqual(event["parameters"], {"topic": "environment", "year": "2026"})


if __name__ == "__main__":
    unittest.main()
