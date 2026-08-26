from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf"
MANIFEST = ROOT / "travel_eval" / "fixtures" / "documents" / "manifest.json"

NAVY = colors.HexColor("#16324F")
BLUE = colors.HexColor("#2C7BE5")
PALE_BLUE = colors.HexColor("#EAF2FC")
TEXT = colors.HexColor("#233044")
MUTED = colors.HexColor("#65758B")
BORDER = colors.HexColor("#D6DEE8")


def build_styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="Brand",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=NAVY,
            spaceAfter=2 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Meta",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            textColor=MUTED,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=NAVY,
            spaceBefore=5 * mm,
            spaceAfter=2 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodySmall",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=TEXT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Route",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=20,
            textColor=NAVY,
        )
    )
    return styles


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(BORDER)
    canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, "Synthetic evaluation fixture - not valid for travel")
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def itinerary_header(styles, brand: str, subtitle: str, pnr: str, issued: str):
    left = [Paragraph(brand, styles["Brand"]), Paragraph(subtitle, styles["Meta"])]
    right = [
        Paragraph("<b>Booking reference</b>", ParagraphStyle("r1", parent=styles["Meta"], alignment=TA_RIGHT)),
        Paragraph(pnr, ParagraphStyle("r2", parent=styles["Route"], alignment=TA_RIGHT, fontSize=15)),
        Paragraph(f"Issued {issued}", ParagraphStyle("r3", parent=styles["Meta"], alignment=TA_RIGHT)),
    ]
    table = Table([[left, right]], colWidths=[110 * mm, 56 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * mm),
                ("LINEBELOW", (0, 0), (-1, -1), 1, BLUE),
            ]
        )
    )
    return table


def segment_card(styles, segment: dict[str, str]):
    route = Paragraph(
        f"{segment['origin']} &nbsp;&nbsp;to&nbsp;&nbsp; {segment['destination']}",
        styles["Route"],
    )
    flight = Paragraph(
        f"<b>{segment['flight']}</b><br/><font color='#65758B'>{segment['carrier']}</font>",
        styles["BodySmall"],
    )
    date = Paragraph(f"<b>{segment['date']}</b><br/>{segment['duration']}", styles["BodySmall"])
    top = Table([[route, flight, date]], colWidths=[70 * mm, 42 * mm, 44 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    movements = Table(
        [
            [
                Paragraph("DEPARTURE", styles["Meta"]),
                Paragraph("ARRIVAL", styles["Meta"]),
                Paragraph("CABIN", styles["Meta"]),
            ],
            [
                Paragraph(f"<b>{segment['depart_time']}</b><br/>{segment['depart_place']}", styles["BodySmall"]),
                Paragraph(f"<b>{segment['arrive_time']}</b><br/>{segment['arrive_place']}", styles["BodySmall"]),
                Paragraph(f"<b>{segment['cabin']}</b><br/>{segment['status']}", styles["BodySmall"]),
            ],
        ],
        colWidths=[62 * mm, 62 * mm, 32 * mm],
    )
    movements.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, BORDER),
            ]
        )
    )
    outer = Table([[top], [movements]], colWidths=[166 * mm])
    outer.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ]
        )
    )
    return KeepTogether(outer)


def build_itinerary(path: Path, brand: str, pnr: str, issued: str, passenger: str, segments: list[dict[str, str]]):
    styles = build_styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=22 * mm,
        title=f"{brand} itinerary {pnr}",
        author="Travel evaluation fixture generator",
    )
    story = [
        itinerary_header(styles, brand, "E-ticket itinerary and receipt", pnr, issued),
        Spacer(1, 5 * mm),
        Table(
            [[Paragraph("PASSENGER", styles["Meta"]), Paragraph("TICKET STATUS", styles["Meta"])],
             [Paragraph(f"<b>{passenger}</b>", styles["BodySmall"]), Paragraph("<b>CONFIRMED</b>", styles["BodySmall"])]],
            colWidths=[110 * mm, 56 * mm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                    ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            ),
        ),
        Paragraph("Your flights", styles["Section"]),
    ]
    for index, segment in enumerate(segments):
        if index:
            story.append(Spacer(1, 4 * mm))
        story.append(segment_card(styles, segment))
    story.extend(
        [
            Paragraph("Travel notes", styles["Section"]),
            Paragraph(
                "Times shown are local to each airport. Bring valid travel documents and verify live terminal and gate information with the operating carrier.",
                styles["BodySmall"],
            ),
        ]
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def find_font(size: int):
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def build_ambiguous_scan(path: Path):
    width, height = 1654, 2339
    image = Image.new("RGB", (width, height), "#F4F2EC")
    draw = ImageDraw.Draw(image)
    title = find_font(52)
    heading = find_font(32)
    body = find_font(27)
    small = find_font(22)

    draw.rectangle((90, 80, width - 90, 250), fill="#173A5E")
    draw.text((130, 118), "QUICKWING - ITINERARY RECEIPT", font=title, fill="white")
    draw.text((110, 315), "BOOKING REFERENCE", font=small, fill="#5E6875")
    draw.rectangle((450, 300, 760, 352), fill="#1B1B1B")
    draw.text((110, 405), "PASSENGER: SAMPLE / TRAVELLER", font=body, fill="#22272E")

    draw.line((110, 495, width - 110, 495), fill="#AEB7C2", width=3)
    draw.text((110, 545), "FLIGHT", font=small, fill="#5E6875")
    draw.text((110, 590), "QW 7?4", font=heading, fill="#343A40")
    draw.text((430, 545), "DATE", font=small, fill="#5E6875")
    draw.text((430, 590), "18 OCT 2026", font=heading, fill="#343A40")
    draw.text((850, 545), "STATUS", font=small, fill="#5E6875")
    draw.text((850, 590), "CONFIRMED", font=heading, fill="#343A40")

    draw.text((110, 760), "LONDON (LHR)", font=heading, fill="#173A5E")
    draw.text((110, 820), "Departure 07:4?  Terminal 3", font=body, fill="#22272E")
    draw.text((110, 970), "LISBON (LIS)", font=heading, fill="#173A5E")
    draw.text((110, 1030), "Arrival 10:25  Terminal 1", font=body, fill="#22272E")

    draw.rectangle((105, 1200, width - 105, 1410), outline="#B9A36A", width=4, fill="#FFF8DF")
    draw.text((140, 1245), "Some characters are unclear in this scan.", font=body, fill="#5C4B19")
    draw.text((140, 1300), "Verify the flight number and departure time.", font=body, fill="#5C4B19")

    draw.text((110, 2185), "SYNTHETIC TEST FIXTURE - NOT VALID FOR TRAVEL", font=small, fill="#6D747C")
    image = image.rotate(0.35, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="#E8E5DE")
    image = image.filter(ImageFilter.GaussianBlur(radius=0.7))
    image.save(path, "PDF", resolution=150.0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    direct = OUTPUT / "synthetic_direct_eticket.pdf"
    connection = OUTPUT / "synthetic_connection_itinerary.pdf"
    ambiguous = OUTPUT / "redacted_ambiguous_scan.pdf"

    build_itinerary(
        direct,
        brand="NIMBUS AIR",
        pnr="NX4K7Q",
        issued="25 Aug 2026",
        passenger="A. TRAVELLER",
        segments=[
            {
                "origin": "LHR",
                "destination": "AMS",
                "flight": "NB 204",
                "carrier": "Nimbus Air",
                "date": "15 Sep 2026",
                "duration": "1h 15m",
                "depart_time": "09:20",
                "depart_place": "London Heathrow - Terminal 5",
                "arrive_time": "11:35",
                "arrive_place": "Amsterdam Schiphol - Terminal 1",
                "cabin": "Economy",
                "status": "Confirmed",
            }
        ],
    )
    build_itinerary(
        connection,
        brand="SKYBRIDGE",
        pnr="SB8M2P",
        issued="25 Aug 2026",
        passenger="A. TRAVELLER",
        segments=[
            {
                "origin": "MAN",
                "destination": "FRA",
                "flight": "SB 410",
                "carrier": "SkyBridge",
                "date": "20 Sep 2026",
                "duration": "1h 45m",
                "depart_time": "08:10",
                "depart_place": "Manchester - Terminal 2",
                "arrive_time": "10:55",
                "arrive_place": "Frankfurt - Terminal 1",
                "cabin": "Economy",
                "status": "Confirmed",
            },
            {
                "origin": "FRA",
                "destination": "ATH",
                "flight": "SB 882",
                "carrier": "SkyBridge",
                "date": "20 Sep 2026",
                "duration": "2h 50m",
                "depart_time": "12:10",
                "depart_place": "Frankfurt - Terminal 1",
                "arrive_time": "16:00",
                "arrive_place": "Athens - Main Terminal",
                "cabin": "Economy",
                "status": "Confirmed",
            },
        ],
    )
    build_ambiguous_scan(ambiguous)

    manifest = {
        "schema_version": "1.0.0",
        "generated_by": "tools/generate_pdf_fixtures.py",
        "fixtures": [
            {
                "fixture_id": "doc-direct-clean",
                "path": "output/pdf/synthetic_direct_eticket.pdf",
                "sha256": sha256(direct),
                "expected": "travel_eval/fixtures/documents/expected_direct_itinerary.json",
                "classification": "synthetic-clean-pdf",
            },
            {
                "fixture_id": "doc-connection-clean",
                "path": "output/pdf/synthetic_connection_itinerary.pdf",
                "sha256": sha256(connection),
                "expected": "travel_eval/fixtures/documents/expected_connection_itinerary.json",
                "classification": "synthetic-clean-pdf",
            },
            {
                "fixture_id": "doc-ambiguous-scan",
                "path": "output/pdf/redacted_ambiguous_scan.pdf",
                "sha256": sha256(ambiguous),
                "expected": "travel_eval/fixtures/documents/expected_ambiguous_parse.json",
                "ocr_response": "travel_eval/fixtures/documents/mistral_ocr_ambiguous_response.json",
                "classification": "synthetic-raster-scan",
            },
        ],
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
