from __future__ import annotations

import csv
import hashlib
import json
import unittest
from pathlib import Path

from PIL import Image
from PyPDF2 import PdfReader


ROOT = (
    Path(__file__).resolve().parents[1]
    / "research_archive"
)


class ResearchArchiveAssetTests(unittest.TestCase):
    def test_original_masters_and_delivery_images_are_valid(self) -> None:
        masters = (
            "coastal-aerial-master.png",
            "field-observation-master.png",
            "sensor-array-master.png",
            "web-evidence-master.png",
        )
        for name in masters:
            with self.subTest(master=name):
                with Image.open(ROOT / "static" / "source" / name) as image:
                    self.assertGreaterEqual(image.width, 1500)
                    self.assertGreaterEqual(image.height, 900)
                    self.assertEqual(image.format, "PNG")

        deliveries = {
            "archive-identity.png": ("PNG", (160, 160)),
            "coastal-observation.webp": ("WEBP", (1600, 1000)),
            "field-research.jpg": ("JPEG", (1600, 1000)),
            "sensor-array.png": ("PNG", (1600, 1000)),
            "web-evidence-figure.jpg": ("JPEG", (1600, 1000)),
        }
        for name, (expected_format, expected_size) in deliveries.items():
            with self.subTest(delivery=name):
                with Image.open(ROOT / "static" / "images" / name) as image:
                    self.assertEqual(image.format, expected_format)
                    self.assertEqual(image.size, expected_size)
                    image.verify()

    def test_pdfs_are_readable_and_have_stable_metadata(self) -> None:
        expected = {
            "web-evidence.pdf": (3, "Preserving Web Evidence"),
            "methods-supplement.pdf": (
                2,
                "Regional Observation Series - Methods Supplement",
            ),
        }
        for name, (page_count, title) in expected.items():
            with self.subTest(document=name):
                path = ROOT / "static" / "documents" / name
                self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
                reader = PdfReader(path)
                self.assertEqual(len(reader.pages), page_count)
                self.assertEqual(reader.metadata.title, title)
                self.assertTrue(all((page.extract_text() or "").strip() for page in reader.pages))

    def test_csv_and_json_deliver_the_same_ordered_observations(self) -> None:
        data_root = ROOT / "static" / "data"
        with (data_root / "regional-observations.csv").open(
            newline="",
            encoding="utf-8",
        ) as handle:
            csv_rows = list(csv.DictReader(handle))
        json_rows = json.loads(
            (data_root / "regional-observations.json").read_text(encoding="utf-8")
        )["records"]
        normalized_json = [
            {key: str(value) for key, value in row.items()}
            for row in json_rows
        ]
        self.assertEqual(csv_rows, normalized_json)

    def test_asset_builder_is_byte_deterministic(self) -> None:
        from evaluation.cache_recovery.research_archive.build_assets import build_assets

        outputs = tuple((ROOT / "static" / folder / name) for folder, name in (
            ("images", "archive-identity.png"),
            ("images", "coastal-observation.webp"),
            ("images", "field-research.jpg"),
            ("images", "sensor-array.png"),
            ("images", "web-evidence-figure.jpg"),
            ("documents", "web-evidence.pdf"),
            ("documents", "methods-supplement.pdf"),
            ("data", "regional-observations.csv"),
            ("data", "regional-observations.json"),
        ))
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in outputs
        }
        build_assets()
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in outputs
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
