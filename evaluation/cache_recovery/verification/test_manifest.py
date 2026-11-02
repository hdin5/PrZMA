from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class ResearchArchiveManifestTests(unittest.TestCase):
    def test_interactive_resource_ids_use_the_neutral_schema_taxonomy(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        interactive = {
            item.resource_id
            for item in resource_specs()
            if item.classification == "interactive"
        }
        self.assertEqual(
            interactive,
            {
                "discover.html.01",
                "discover.css.01",
                "discover.html.02",
                "discover.js.01",
                "discover.json.01",
                "discover.webp.01",
                "discover.jpeg.01",
                "discover.png.01",
                "discover.html.03",
                "discover.css.02",
                "discover.js.02",
                "discover.json.02",
                "discover.html.04",
                "inquiry.html.01",
                "inquiry.css.01",
                "inquiry.js.01",
                "inquiry.json.01",
                "retrieve.html.01",
                "retrieve.css.01",
                "retrieve.js.01",
                "retrieve.jpeg.01",
                "retrieve.pdf.01",
                "retrieve.pdf.02",
                "retrieve.html.02",
                "retrieve.css.02",
                "retrieve.js.02",
                "retrieve.json.01",
                "retrieve.csv.01",
                "retrieve.json.02",
            },
        )
        self.assertEqual(
            {
                item.journey
                for item in resource_specs()
                if item.classification == "interactive"
            },
            {"discover", "inquiry", "retrieve"},
        )
        forbidden = (
            "atlas",
            "map",
            "gallery",
            "collection",
            "search",
            "report",
            "dataset",
            "reuse",
        )
        self.assertFalse(
            {
                resource_id
                for resource_id in interactive
                if any(token in resource_id for token in forbidden)
            }
        )

    def test_behavior_ids_use_generic_interaction_semantics(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import behavior_specs

        behaviors = behavior_specs()
        self.assertEqual(
            {item.behavior_id for item in behaviors},
            {
                "discover.navigation.01",
                "discover.navigation.02",
                "discover.dynamic_request.01",
                "discover.viewport_reveal.01",
                "discover.embedded_open.01",
                "discover.navigation.03",
                "inquiry.submit.01",
                "inquiry.parameter_commit.01",
                "retrieve.navigation.01",
                "retrieve.inline_open.01",
                "retrieve.download.01",
                "retrieve.navigation.02",
                "retrieve.parameter_request.01",
                "retrieve.download.02",
                "retrieve.download.03",
            },
        )
        self.assertEqual(
            {item.journey for item in behaviors},
            {"discover", "inquiry", "retrieve"},
        )

    def test_interactive_resources_have_finite_unique_ids(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        resources = resource_specs()
        resource_ids = [(item.resource_id, item.variant_id) for item in resources]

        self.assertEqual(len(resource_ids), len(set(resource_ids)))
        self.assertEqual(
            {item.classification for item in resources},
            {"passive", "interactive"},
        )
        self.assertTrue(
            all(item.trigger for item in resources if item.classification == "interactive")
        )

    def test_resource_catalog_has_complete_recovery_contract(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        required_headers = {
            "Cache-Control",
            "ETag",
            "Last-Modified",
            "Content-Encoding",
            "Vary",
        }
        for resource in resource_specs():
            with self.subTest(resource=resource.resource_id, variant=resource.variant_id):
                self.assertTrue(resource.url.startswith("/"))
                self.assertIn(resource.method, {"GET", "POST"})
                self.assertGreaterEqual(resource.status, 200)
                self.assertTrue(resource.media_type)
                self.assertTrue(resource.content_encoding)
                self.assertGreater(resource.decoded_size, 0)
                self.assertGreater(resource.encoded_size, 0)
                self.assertEqual(len(resource.decoded_sha256), 64)
                self.assertEqual(len(resource.encoded_sha256), 64)
                self.assertEqual(set(dict(resource.cache_headers)), required_headers)
                self.assertGreaterEqual(resource.request_order, 1)
                self.assertIsInstance(resource.parameter_variant, tuple)

    def test_research_archive_entry_has_exactly_four_passive_resources(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import resource_specs

        passive = [item for item in resource_specs() if item.classification == "passive"]
        self.assertEqual(
            {item.resource_id for item in passive},
            {
                "passive.home_html",
                "passive.base_css",
                "passive.base_js",
                "passive.identity_png",
            },
        )

    def test_each_behavior_has_resource_evidence(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import (
            behavior_specs,
            resource_specs,
        )

        resource_ids = {item.resource_id for item in resource_specs()}
        for behavior in behavior_specs():
            self.assertTrue(behavior.evidence_resource_ids, behavior.behavior_id)
            self.assertLessEqual(set(behavior.evidence_resource_ids), resource_ids)

    def test_every_interactive_resource_is_linked_to_a_behavior(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import (
            behavior_specs,
            resource_specs,
        )

        interactive = {
            item.resource_id
            for item in resource_specs()
            if item.classification == "interactive"
        }
        evidenced = {
            resource_id
            for behavior in behavior_specs()
            for resource_id in behavior.evidence_resource_ids
        }
        self.assertEqual(interactive - evidenced, set())

    def test_public_manifests_are_deterministic(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import (
            write_public_manifests,
        )

        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = write_public_manifests(Path(first_tmp))
            second = write_public_manifests(Path(second_tmp))

            self.assertEqual(
                json.loads(first["resources"].read_text(encoding="utf-8")),
                json.loads(second["resources"].read_text(encoding="utf-8")),
            )
            self.assertEqual(
                json.loads(first["behaviors"].read_text(encoding="utf-8")),
                json.loads(second["behaviors"].read_text(encoding="utf-8")),
            )

    def test_committed_manifests_match_the_current_definition(self) -> None:
        from evaluation.cache_recovery.research_archive.manifest import (
            write_public_manifests,
        )

        committed_root = (
            Path(__file__).resolve().parents[1]
            / "research_archive"
        )
        with tempfile.TemporaryDirectory() as tmp:
            generated = write_public_manifests(Path(tmp))
            self.assertEqual(
                json.loads((committed_root / "resource_manifest.json").read_text(encoding="utf-8")),
                json.loads(generated["resources"].read_text(encoding="utf-8")),
            )
            self.assertEqual(
                json.loads((committed_root / "behavior_manifest.json").read_text(encoding="utf-8")),
                json.loads(generated["behaviors"].read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
