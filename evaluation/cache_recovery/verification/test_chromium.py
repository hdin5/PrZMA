from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright


CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class ResearchArchiveChromiumTests(unittest.TestCase):
    @contextmanager
    def _browser(self):
        if not CHROME.is_file():
            self.skipTest(f"System Chrome is unavailable: {CHROME}")
        with tempfile.TemporaryDirectory() as profile_dir:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    executable_path=str(CHROME),
                    headless=True,
                    args=(
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--no-first-run",
                    ),
                )
                try:
                    yield context
                finally:
                    context.close()

    @staticmethod
    def _resource_ids(server, urls: list[str]) -> set[str]:
        resource_ids: set[str] = set()
        for url in urls:
            parsed = urlsplit(url)
            if f"{parsed.scheme}://{parsed.netloc}" != server.base_url:
                continue
            resource = server.definition.resource_for("GET", parsed.path)
            if resource is not None:
                resource_ids.add(resource.resource_id)
        return resource_ids

    def _assert_server_requests_appear_in_browser_trace(
        self,
        server,
        run,
        responses: list[str],
        *,
        request_start: int,
        response_start: int,
    ) -> None:
        server_urls = [
            str(event["request_url"])
            for event in run.requests[request_start:]
        ]
        request_orders = [
            int(event["request_order"])
            for event in run.requests[request_start:]
        ]
        self.assertEqual(
            request_orders,
            sorted(request_orders),
            "Chromium requests violated the behavior's declared logical order",
        )
        browser_urls = []
        for url in responses[response_start:]:
            parsed = urlsplit(url)
            if f"{parsed.scheme}://{parsed.netloc}" != server.base_url:
                continue
            if server.definition.resource_for("GET", parsed.path) is not None:
                browser_urls.append(url)
        missing = Counter(server_urls) - Counter(browser_urls)
        self.assertEqual(
            missing,
            Counter(),
            "A server ground-truth request has no matching Chromium response event",
        )

    @staticmethod
    def _open_entry(page: Page, server, run_id: str, responses: list[str]) -> None:
        page.on("response", lambda response: responses.append(response.url))
        page.goto(f"{server.base_url}/start?run_id={run_id}", wait_until="networkidle")

    def test_initial_screen_requests_exactly_the_four_passive_resources(self) -> None:
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        with running_research_archive_server(host="127.0.0.1", port=0) as server:
            server.register_run("chromium-passive")
            with self._browser() as context:
                page = context.pages[0]
                responses: list[str] = []
                self._open_entry(page, server, "chromium-passive", responses)

                expected = {
                    "passive.home_html",
                    "passive.base_css",
                    "passive.base_js",
                    "passive.identity_png",
                }
                self.assertEqual(server.runs["chromium-passive"].requested_ids, expected)
                self.assertEqual(self._resource_ids(server, responses), expected)

    def test_collection_actions_generate_only_their_declared_resources(self) -> None:
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        with running_research_archive_server(host="127.0.0.1", port=0) as server:
            run = server.register_run("chromium-collection")
            behaviors = {
                item.behavior_id: set(item.evidence_resource_ids)
                for item in server.definition.behaviors
            }
            with self._browser() as context:
                page = context.pages[0]
                responses: list[str] = []
                self._open_entry(page, server, run.run_id, responses)

                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                page.locator("a.collection-card.sea").click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    set(run.requested_ids) - before,
                    behaviors["discover.navigation.02"],
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server,
                    run,
                    responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                with page.expect_response("**/api/collections/environmental-signals"):
                    page.locator("#load-metadata").click()
                self.assertEqual(
                    set(run.requested_ids) - before,
                    behaviors["discover.dynamic_request.01"],
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server,
                    run,
                    responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                page.locator(".lazy-gallery").scroll_into_view_if_needed()
                for image in page.locator(".lazy-gallery img").all():
                    image.scroll_into_view_if_needed()
                page.wait_for_function(
                    "() => [...document.querySelectorAll('.lazy-gallery img')].every(img => img.complete && img.currentSrc)"
                )
                page.wait_for_function(
                    """() => [...document.querySelectorAll('.lazy-gallery img')].every(
                      img => img.classList.contains('loaded') && getComputedStyle(img).opacity === '1'
                    )"""
                )
                gallery_state = page.locator(".lazy-gallery img").evaluate_all(
                    """images => images.map(image => ({
                      loaded: image.classList.contains('loaded'),
                      opacity: getComputedStyle(image).opacity,
                      naturalWidth: image.naturalWidth,
                    }))"""
                )
                self.assertEqual(
                    gallery_state,
                    [
                        {"loaded": True, "opacity": "1", "naturalWidth": 1600},
                        {"loaded": True, "opacity": "1", "naturalWidth": 1600},
                        {"loaded": True, "opacity": "1", "naturalWidth": 1600},
                    ],
                    "Lazy gallery resources loaded but were not visibly revealed",
                )
                self.assertEqual(
                    set(run.requested_ids) - before,
                    behaviors["discover.viewport_reveal.01"],
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server,
                    run,
                    responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                with page.expect_response("**/api/map/observations"):
                    page.locator("#open-map").click()
                self.assertEqual(
                    set(run.requested_ids) - before,
                    behaviors["discover.embedded_open.01"],
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server,
                    run,
                    responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                public_paths = {
                    urlsplit(url).path
                    for url in responses
                    if url.startswith(server.base_url)
                }
                self.assertFalse(
                    public_paths
                    & {"/manifest", "/resource_manifest.json", "/oracle", "/control"}
                )

    def test_search_report_and_dataset_actions_have_manifest_evidence(self) -> None:
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        with running_research_archive_server(host="127.0.0.1", port=0) as server:
            run = server.register_run("chromium-reuse")
            with self._browser() as context:
                page = context.pages[0]
                responses: list[str] = []
                self._open_entry(page, server, run.run_id, responses)

                page.locator("#archive-search").fill("coastal")
                page.locator(".archive-search button[type=submit]").click()
                page.wait_for_load_state("networkidle")
                self.assertTrue(
                    {"inquiry.html.01", "inquiry.css.01", "inquiry.js.01"}
                    <= run.requested_ids
                )

                page.locator("#topic-filter").select_option("environment")
                page.locator("#year-filter").select_option("2026")
                page.locator("#format-filter").select_option("report")
                page.locator("#sort-filter").select_option("newest")
                with page.expect_response("**/api/search?**"):
                    page.locator("#apply-filters").click()
                self.assertIn("inquiry.json.01", run.requested_ids)

                page.goto(server.base_url, wait_until="networkidle")
                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                page.locator("a.button", has_text="Open report").click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    set(run.requested_ids) - before,
                    {"retrieve.html.01", "retrieve.css.01", "retrieve.js.01", "retrieve.jpeg.01"},
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server, run, responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                before = set(run.requested_ids)
                with page.expect_response("**/documents/web-evidence.pdf"):
                    page.locator("#open-report").click()
                self.assertEqual(set(run.requested_ids) - before, {"retrieve.pdf.01"})

                before = set(run.requested_ids)
                with page.expect_download() as download_info:
                    page.locator("a.quiet-button", has_text="Download methods").click()
                download_info.value.cancel()
                self.assertEqual(set(run.requested_ids) - before, {"retrieve.pdf.02"})
                requests_after_methods_fill = len(run.requests)
                with page.expect_download() as cached_download_info:
                    page.locator("a.quiet-button", has_text="Download methods").click()
                cached_download_info.value.cancel()
                self.assertEqual(
                    len(run.requests),
                    requests_after_methods_fill,
                    "The user download action did not reuse the Chromium HTTP cache",
                )

                page.goto(server.base_url, wait_until="networkidle")
                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                page.locator("a", has_text="Explore data").click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    set(run.requested_ids) - before,
                    {"retrieve.html.02", "retrieve.css.02", "retrieve.js.02"},
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server, run, responses,
                    request_start=request_start,
                    response_start=response_start,
                )
                page.locator("#region-select").select_option("estuary")
                page.locator("#measure-select").select_option("salinity")
                before = set(run.requested_ids)
                with page.expect_response("**/api/datasets/regional-observations?**"):
                    page.locator("#load-preview").click()
                self.assertEqual(set(run.requested_ids) - before, {"retrieve.json.01"})

                before = set(run.requested_ids)
                with page.expect_download() as csv_download:
                    page.locator('a[href="/downloads/regional-observations.csv"]').click()
                csv_download.value.cancel()
                self.assertEqual(set(run.requested_ids) - before, {"retrieve.csv.01"})

                before = set(run.requested_ids)
                with page.expect_download() as json_download:
                    page.locator('a[href="/downloads/regional-observations.json"]').click()
                json_download.value.cancel()
                self.assertEqual(set(run.requested_ids) - before, {"retrieve.json.02"})

                requested_urls = [str(event["request_url"]) for event in run.requests]
                self.assertTrue(any("query=coastal" in url for url in requested_urls))
                self.assertTrue(any("region=estuary" in url for url in requested_urls))

    def test_collections_index_and_map_navigation_are_user_triggered(self) -> None:
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        with running_research_archive_server(host="127.0.0.1", port=0) as server:
            run = server.register_run("chromium-navigation")
            with self._browser() as context:
                page = context.pages[0]
                responses: list[str] = []
                self._open_entry(page, server, run.run_id, responses)

                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                primary_navigation = page.get_by_role("navigation", name="Primary navigation")
                primary_navigation.get_by_role("link", name="Collections", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    set(run.requested_ids) - before,
                    {"discover.html.01", "discover.css.01"},
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server, run, responses,
                    request_start=request_start,
                    response_start=response_start,
                )

                page.goto(server.base_url, wait_until="networkidle")
                before = set(run.requested_ids)
                request_start, response_start = len(run.requests), len(responses)
                primary_navigation = page.get_by_role("navigation", name="Primary navigation")
                primary_navigation.get_by_role("link", name="Map", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(
                    set(run.requested_ids) - before,
                    {"discover.html.04", "discover.css.02"},
                )
                self._assert_server_requests_appear_in_browser_trace(
                    server, run, responses,
                    request_start=request_start,
                    response_start=response_start,
                )

    def test_all_canonical_resources_are_reused_from_chromium_disk_cache(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs
        from evaluation.cache_recovery.research_archive.server import (
            running_research_archive_server,
        )

        if not CHROME.is_file():
            self.skipTest(f"System Chrome is unavailable: {CHROME}")
        with tempfile.TemporaryDirectory() as profile_dir:
            profile = Path(profile_dir)
            with running_research_archive_server(host="127.0.0.1", port=0) as server:
                run = server.register_run("chromium-disk")
                canonical_urls = [
                    f"{server.base_url}{spec.url}"
                    for spec in resource_specs(server.definition)
                    if spec.cache_eligible
                ]
                with sync_playwright() as playwright:
                    launch_options = {
                        "user_data_dir": str(profile),
                        "executable_path": str(CHROME),
                        "headless": True,
                        "args": (
                            "--disable-background-networking",
                            "--disable-component-update",
                            "--disable-default-apps",
                            "--no-first-run",
                        ),
                    }
                    first = playwright.chromium.launch_persistent_context(**launch_options)
                    first_page = first.pages[0]
                    first_page.goto(
                        f"{server.base_url}/start?run_id={run.run_id}",
                        wait_until="networkidle",
                    )
                    first_page.evaluate(
                        """async (urls) => {
                          for (const url of urls) {
                            const response = await fetch(url, {cache: 'default'});
                            if (!response.ok) throw new Error(`${response.status} ${url}`);
                            await response.arrayBuffer();
                          }
                        }""",
                        canonical_urls,
                    )
                    first.close()

                    cache_data = profile / "Default" / "Cache" / "Cache_Data"
                    self.assertTrue(
                        cache_data.is_dir(),
                        f"Chromium HTTP disk-cache directory is absent: {cache_data}",
                    )
                    cache_files = [path for path in cache_data.iterdir() if path.is_file()]
                    self.assertTrue(cache_files, "Chromium created no HTTP disk-cache files")
                    self.assertGreater(
                        sum(path.stat().st_size for path in cache_files),
                        0,
                        "Chromium cache files are empty",
                    )

                    requests_after_fill = len(run.requests)
                    second = playwright.chromium.launch_persistent_context(**launch_options)
                    second_page = second.pages[0]
                    cdp = second.new_cdp_session(second_page)
                    responses: list[dict] = []
                    cdp.on("Network.responseReceived", lambda event: responses.append(event))
                    cdp.send("Network.enable")
                    second_page.goto(server.base_url, wait_until="networkidle")
                    second_page.evaluate(
                        """async (urls) => {
                          for (const url of urls) {
                            const response = await fetch(url, {cache: 'default'});
                            if (!response.ok) throw new Error(`${response.status} ${url}`);
                            await response.arrayBuffer();
                          }
                        }""",
                        canonical_urls,
                    )
                    second.close()

                disk_urls = {
                    event["response"]["url"]
                    for event in responses
                    if event["response"].get("fromDiskCache") is True
                }
                self.assertEqual(
                    set(canonical_urls) - disk_urls,
                    set(),
                    "Some canonical resources were not reused from Chromium disk cache",
                )
                self.assertEqual(
                    len(run.requests),
                    requests_after_fill,
                    "The server received requests during the disk-cache-only replay",
                )


if __name__ == "__main__":
    unittest.main()
