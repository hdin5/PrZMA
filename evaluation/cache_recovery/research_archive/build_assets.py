from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from shutil import copyfile
from textwrap import wrap

from PIL import Image, ImageDraw
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "static" / "source"
IMAGES = ROOT / "static" / "images"
DOCUMENTS = ROOT / "static" / "documents"
DATA = ROOT / "static" / "data"

INK = HexColor("#10263B")
TEAL = HexColor("#2D6B6F")
AMBER = HexColor("#C78B2B")
PAPER = HexColor("#F6F2E9")
MUTED = HexColor("#5F6D75")


OBSERVATIONS = (
    {"record_id": "RA-OBS-001", "date": "2026-03-12", "region": "north-coast", "temperature_c": 12.8, "salinity_psu": 31.4, "turbidity_ntu": 4.2},
    {"record_id": "RA-OBS-002", "date": "2026-03-12", "region": "estuary", "temperature_c": 13.6, "salinity_psu": 18.7, "turbidity_ntu": 12.5},
    {"record_id": "RA-OBS-003", "date": "2026-03-12", "region": "harbor", "temperature_c": 14.1, "salinity_psu": 27.2, "turbidity_ntu": 8.8},
    {"record_id": "RA-OBS-004", "date": "2026-04-18", "region": "north-coast", "temperature_c": 14.7, "salinity_psu": 32.0, "turbidity_ntu": 3.9},
    {"record_id": "RA-OBS-005", "date": "2026-04-18", "region": "estuary", "temperature_c": 15.3, "salinity_psu": 19.4, "turbidity_ntu": 11.2},
    {"record_id": "RA-OBS-006", "date": "2026-04-18", "region": "harbor", "temperature_c": 15.8, "salinity_psu": 28.1, "turbidity_ntu": 7.6},
)


def _fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_ratio = size[0] / size[1]
    source_ratio = image.width / image.height
    if source_ratio > target_ratio:
        crop_width = round(image.height * target_ratio)
        left = (image.width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, image.height))
    else:
        crop_height = round(image.width / target_ratio)
        top = (image.height - crop_height) // 2
        image = image.crop((0, top, image.width, top + crop_height))
    return image.resize(size, Image.Resampling.LANCZOS)


def _build_images() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    field = _fit(Image.open(SOURCE / "field-observation-master.png").convert("RGB"), (1600, 1000))
    field.save(IMAGES / "field-research.jpg", "JPEG", quality=88, optimize=True, progressive=False, subsampling=1)

    coast = _fit(Image.open(SOURCE / "coastal-aerial-master.png").convert("RGB"), (1600, 1000))
    coast.save(IMAGES / "coastal-observation.webp", "WEBP", quality=86, method=6, exact=True)

    sensor = _fit(Image.open(SOURCE / "sensor-array-master.png").convert("RGB"), (1600, 1000))
    sensor.save(IMAGES / "sensor-array.png", "PNG", optimize=False, compress_level=9)

    figure = _fit(Image.open(SOURCE / "web-evidence-master.png").convert("RGB"), (1600, 1000))
    figure.save(IMAGES / "web-evidence-figure.jpg", "JPEG", quality=90, optimize=True, progressive=False, subsampling=1)

    mark = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.rounded_rectangle((8, 8, 152, 152), radius=34, fill="#10263B")
    draw.rounded_rectangle((34, 34, 126, 126), radius=14, outline="#E8DCC3", width=7)
    draw.rectangle((45, 55, 115, 67), fill="#C78B2B")
    draw.rectangle((45, 77, 105, 89), fill="#2D8587")
    draw.rectangle((45, 99, 115, 111), fill="#E8DCC3")
    mark.save(IMAGES / "archive-identity.png", "PNG", optimize=False, compress_level=9)


def _build_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fields = (
        "record_id",
        "date",
        "region",
        "temperature_c",
        "salinity_psu",
        "turbidity_ntu",
    )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(OBSERVATIONS)
    (DATA / "regional-observations.csv").write_text(
        buffer.getvalue(),
        encoding="utf-8",
        newline="",
    )
    (DATA / "regional-observations.json").write_text(
        json.dumps(
            {
                "dataset": "Regional Observation Series",
                "schema_version": 1,
                "units": {
                    "temperature_c": "degrees Celsius",
                    "salinity_psu": "practical salinity units",
                    "turbidity_ntu": "nephelometric turbidity units",
                },
                "records": OBSERVATIONS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _header(pdf: canvas.Canvas, title: str, section: str, page_number: int) -> None:
    width, height = A4
    pdf.setFillColor(INK)
    pdf.rect(0, height - 72, width, 72, fill=1, stroke=0)
    pdf.setFillColor(PAPER)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(42, height - 31, "RESEARCH ARCHIVE")
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(width - 42, height - 31, section.upper())
    pdf.setStrokeColor(AMBER)
    pdf.setLineWidth(3)
    pdf.line(42, height - 58, 145, height - 58)
    pdf.setFillColor(INK)
    pdf.setFont("Times-Bold", 19)
    pdf.drawString(42, height - 105, title)
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(width - 42, 32, f"Research Archive  |  {page_number}")


def _paragraph(pdf: canvas.Canvas, text: str, x: float, y: float, *, width_chars: int = 88, leading: float = 14) -> float:
    pdf.setFillColor(HexColor("#263B48"))
    pdf.setFont("Helvetica", 9.5)
    for line in wrap(text, width=width_chars):
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _section(pdf: canvas.Canvas, label: str, title: str, y: float) -> float:
    pdf.setFillColor(TEAL)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(42, y, label.upper())
    pdf.setFillColor(INK)
    pdf.setFont("Times-Bold", 15)
    pdf.drawString(42, y - 22, title)
    return y - 45


def _build_report() -> None:
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    copyfile(
        SOURCE / "web-evidence-master.pdf",
        DOCUMENTS / "web-evidence.pdf",
    )


def _build_methods() -> None:
    output = DOCUMENTS / "methods-supplement.pdf"
    pdf = canvas.Canvas(str(output), pagesize=A4, pageCompression=1, invariant=1)
    pdf.setTitle("Regional Observation Series - Methods Supplement")
    pdf.setAuthor("Research Archive Environmental Records Group")
    pdf.setSubject("Field collection, quality assurance, and published variable definitions")
    _, height = A4
    _header(pdf, "Regional Observation Series", "Methods supplement", 1)
    y = _section(pdf, "Scope", "Field collection and normalization", height - 145)
    y = _paragraph(pdf, "Observations were recorded at North Coast, Estuary, and Harbor sites using a shared field sheet and synchronized instrument clocks. Temperature, salinity, and turbidity values are reported in the units declared by the dataset metadata.", 42, y)
    y -= 22
    y = _section(pdf, "Quality assurance", "Review before publication", y)
    y = _paragraph(pdf, "Each record was checked for identifier uniqueness, valid dates, permitted region labels, expected measurement ranges, and unit consistency. CSV and JSON deliveries are generated from the same ordered record collection.", 42, y)
    y -= 22
    y = _section(pdf, "Reuse", "Stable fields and provenance", y)
    _paragraph(pdf, "The record identifier, observation date, region, and three measurement columns form the stable public schema. Consumers should retain the dataset title, schema version, unit map, and this methods supplement alongside derived results.", 42, y)
    pdf.showPage()
    _header(pdf, "Regional Observation Series", "Field dictionary", 2)
    y = _section(pdf, "Dictionary", "Published variables", height - 145)
    rows = (
        ("record_id", "Stable Research Archive observation identifier"),
        ("date", "Observation date in ISO 8601 calendar format"),
        ("region", "Controlled site label: north-coast, estuary, or harbor"),
        ("temperature_c", "Water temperature in degrees Celsius"),
        ("salinity_psu", "Salinity in practical salinity units"),
        ("turbidity_ntu", "Turbidity in nephelometric turbidity units"),
    )
    for index, (field_name, description) in enumerate(rows):
        fill = Color(0.93, 0.91, 0.86) if index % 2 == 0 else Color(0.98, 0.97, 0.94)
        pdf.setFillColor(fill)
        pdf.rect(42, y - 23, 511, 34, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(54, y - 4, field_name)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(176, y - 4, description)
        y -= 42
    pdf.save()


def build_assets() -> None:
    _build_images()
    _build_data()
    _build_report()
    _build_methods()


if __name__ == "__main__":
    build_assets()
