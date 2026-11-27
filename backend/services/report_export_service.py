from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from reportlab.lib.units import mm
from services.report_generator import _pdf_font, _pdf_text

if TYPE_CHECKING:
    from services.reports_center_service import ReportResult

_MIME_TYPES = {
    "csv": "text/csv",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def export_report_csv(result: ReportResult) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow([_cell(row.get(column)) for column in result.columns])
    return buffer.getvalue().encode("utf-8")


def export_report_json(result: ReportResult) -> bytes:
    payload = asdict(result)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def export_report_xlsx(result: ReportResult) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary_rows: list[tuple[str, Any]] = [
        ("Application", "AI Jobs Interview Assistant"),
        ("Report title", result.title),
        ("Report category", result.category),
        ("Generated at", result.generated_at.strftime("%Y-%m-%d %H:%M:%S")),
        ("Start date", result.start_date.isoformat() if result.start_date else "All"),
        ("End date", result.end_date.isoformat() if result.end_date else "All"),
    ]
    summary_rows.extend((label, value) for label, value in result.summary)
    summary_rows.append(("Applied filters", json.dumps(result.filters, ensure_ascii=False, default=str)))
    for note in result.notes:
        summary_rows.append(("Note", note))
    summary.append(["Field", "Value"])
    for label, value in summary_rows:
        summary.append([label, _cell(value)])
    for cell in summary[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 90
    summary.freeze_panes = "A2"

    details = workbook.create_sheet("Details")
    details.append(result.columns)
    for row in result.rows:
        details.append([_cell(row.get(column)) for column in result.columns])
    for cell in details[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(vertical="center")
    details.freeze_panes = "A2"
    details.auto_filter.ref = details.dimensions
    for index, column in enumerate(result.columns, start=1):
        longest = max(
            [len(column)] + [len(_cell(row.get(column))) for row in result.rows[:200]] or [10]
        )
        details.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 12), 50)

    if result.charts:
        charts = workbook.create_sheet("Charts")
        for chart in result.charts:
            charts.append([chart.title])
            charts.append([chart.x, chart.y])
            for item in chart.data:
                charts.append([_cell(item.get(chart.x)), _cell(item.get(chart.y))])
            charts.append([])
        for cell in charts[1]:
            cell.font = Font(bold=True)
        charts.column_dimensions["A"].width = 36
        charts.column_dimensions["B"].width = 18

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_page(canvas_obj: Any, doc: Any) -> None:
    canvas_obj.saveState()
    canvas_obj.setFont("Helvetica", 8)
    canvas_obj.setFillColorRGB(0.35, 0.35, 0.35)
    canvas_obj.drawString(18 * mm, 12 * mm, "AI Jobs Interview Assistant")
    canvas_obj.drawRightString(doc.pagesize[0] - 18 * mm, 12 * mm, f"Page {canvas_obj.getPageNumber()}")
    canvas_obj.restoreState()


def _pdf_chart(chart: Any, font_name: str, unicode_safe: bool) -> Any:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing

    data = chart.data[:12]
    if not data:
        return None
    values: list[float] = []
    labels: list[str] = []
    for item in data:
        try:
            values.append(float(item.get(chart.y) or 0))
        except (TypeError, ValueError):
            values.append(0.0)
        label = _pdf_text(item.get(chart.x), unicode_safe)
        labels.append(label[:22])
    drawing = Drawing(460, 170)
    bar = VerticalBarChart()
    bar.x = 45
    bar.y = 35
    bar.width = 390
    bar.height = 115
    bar.data = [values]
    bar.categoryAxis.categoryNames = labels
    bar.categoryAxis.labels.fontName = font_name
    bar.categoryAxis.labels.fontSize = 7
    bar.categoryAxis.labels.angle = 30
    bar.categoryAxis.labels.dy = -8
    bar.valueAxis.valueMin = 0
    bar.valueAxis.labels.fontName = font_name
    bar.valueAxis.labels.fontSize = 7
    drawing.add(bar)
    return drawing


def export_report_pdf(result: ReportResult) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name, unicode_safe = _pdf_font()
    title_style = ParagraphStyle(
        "ReportTitle", fontName=font_name, fontSize=19, leading=23, spaceAfter=8
    )
    heading_style = ParagraphStyle(
        "ReportHeading", fontName=font_name, fontSize=12, leading=15, spaceBefore=10, spaceAfter=6
    )
    body_style = ParagraphStyle(
        "ReportBody", fontName=font_name, fontSize=8.5, leading=11, alignment=TA_LEFT
    )
    cell_style = ParagraphStyle("ReportCell", fontName=font_name, fontSize=7, leading=9)
    header_style = ParagraphStyle(
        "ReportHeaderCell", fontName=font_name, fontSize=7, leading=9, textColor=colors.white
    )

    def paragraph(value: Any, style: ParagraphStyle = body_style) -> Paragraph:
        return Paragraph(_pdf_text(value, unicode_safe), style)

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title=result.title,
        author="AI Jobs Interview Assistant",
    )
    story: list[Any] = [paragraph("AI Jobs Interview Assistant", heading_style), paragraph(result.title, title_style)]
    metadata = [
        [paragraph("Report category", header_style), paragraph(result.category, cell_style)],
        [
            paragraph("Generated at", header_style),
            paragraph(result.generated_at.strftime("%Y-%m-%d %H:%M:%S"), cell_style),
        ],
        [
            paragraph("Date range", header_style),
            paragraph(
                f"{result.start_date or 'All'} to {result.end_date or 'All'}", cell_style
            ),
        ],
        [
            paragraph("Applied filters", header_style),
            paragraph(json.dumps(result.filters, ensure_ascii=False, default=str), cell_style),
        ],
    ]
    metadata_table = Table(metadata, colWidths=[38 * mm, 136 * mm])
    metadata_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#1F3A5F")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7C3D0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.extend([metadata_table, paragraph("Summary", heading_style)])
    summary_data = [
        [paragraph("Metric", header_style), paragraph("Value", header_style)],
        *[
            [paragraph(label, cell_style), paragraph(value, cell_style)]
            for label, value in result.summary
        ],
    ]
    summary_table = Table(summary_data, colWidths=[70 * mm, 104 * mm], repeatRows=1)
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7C3D0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(summary_table)

    for chart in result.charts[:2]:
        drawing = _pdf_chart(chart, font_name, unicode_safe)
        if drawing is None:
            continue
        story.append(paragraph(chart.title, heading_style))
        story.append(drawing)
        story.append(Spacer(1, 6))

    story.append(paragraph("Detailed records", heading_style))
    if not result.rows:
        story.append(paragraph("No records match the selected filters.", body_style))
    else:
        header = [paragraph(column, header_style) for column in result.columns]
        detail_data = [header]
        for row in result.rows:
            detail_data.append(
                [paragraph(_cell(row.get(column)), cell_style) for column in result.columns]
            )
        widths = [max(18, min(52, 174 // len(result.columns))) * mm for _ in result.columns]
        detail_table = Table(detail_data, colWidths=widths, repeatRows=1, splitByRow=1)
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C8D0D9")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(detail_table)
    if result.notes:
        story.append(paragraph("Notes", heading_style))
        for note in result.notes:
            story.append(paragraph(note, body_style))

    document.build(story, onFirstPage=_pdf_page, onLaterPages=_pdf_page)
    return buffer.getvalue()


def export_report_bytes(result: ReportResult, export_format: str) -> bytes:
    normalized = str(export_format).lower()
    if normalized == "csv":
        return export_report_csv(result)
    if normalized == "pdf":
        return export_report_pdf(result)
    if normalized == "xlsx":
        return export_report_xlsx(result)
    if normalized == "json":
        return export_report_json(result)
    raise ValueError("Unsupported export format.")


def mime_for_format(export_format: str) -> str:
    return _MIME_TYPES.get(str(export_format).lower(), "application/octet-stream")


def safe_download_name(result: ReportResult, export_format: str) -> str:
    normalized = str(export_format).lower()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", result.title).strip("_").lower() or "report"
    stamp = result.generated_at.strftime("%Y%m%d_%H%M")
    return f"{stem}_{stamp}.{normalized}"
