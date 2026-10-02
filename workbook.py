"""
Builds the 3-sheet Excel workbook from enriched candidate rows.

Sheet 1 — Personal Preferred:    personal email/phone first, work details as backup
Sheet 2 — Professional Preferred: work email/phone required (with source incl.
                                  pattern-guessed/website fallbacks), personal as backup
Sheet 3 — Maximum:                every personal + professional detail

Each enriched row is a dict with the candidate's input fields plus the
enrichment result fields produced by enrich.enrich_candidate().
"""

from copy import copy
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

INPUT_COLS = ["First Name", "Last Name", "Company", "Title", "LinkedIn URL"]

SHEETS = {
    "1 - Personal Preferred": INPUT_COLS + [
        "Personal Email", "Personal Email Source",
        "Personal Phone", "Personal Phone Source",
        "Work Email (backup)", "Work Phone (backup)", "Notes",
    ],
    "2 - Professional Preferred": INPUT_COLS + [
        "Work Email", "Work Email Source",
        "Work Phone", "Work Phone Source",
        "Personal Email (backup)", "Personal Phone (backup)", "Notes",
    ],
    "3 - Maximum (All Details)": INPUT_COLS + [
        "Personal Email", "Personal Email Source",
        "Personal Phone", "Personal Phone Source",
        "Work Email", "Work Email Source",
        "Work Phone", "Work Phone Source", "Notes",
    ],
}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
UNVERIFIED_FILL = PatternFill("solid", fgColor="FFF2CC")


def _style_header(ws, ncols):
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = copy(HEADER_FILL)
        cell.font = copy(HEADER_FONT)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _mark_unverified(cell):
    """Amber highlight for any cell whose value/source is unverified."""
    val = str(cell.value or "")
    if "UNVERIFIED" in val.upper():
        cell.fill = copy(UNVERIFIED_FILL)


def _row_for_sheet(sheet_name: str, cand: dict, res: dict):
    base = [cand.get("first_name", ""), cand.get("last_name", ""),
            cand.get("company", ""), cand.get("title", ""),
            cand.get("linkedin_url", "")]
    if sheet_name.startswith("1 -"):
        return base + [
            res.get("personal_email", ""), res.get("personal_email_source", ""),
            res.get("personal_phone", ""), res.get("personal_phone_source", ""),
            res.get("work_email", ""), res.get("work_phone", ""),
            res.get("notes", ""),
        ]
    if sheet_name.startswith("2 -"):
        return base + [
            res.get("work_email", ""), res.get("work_email_source", ""),
            res.get("work_phone", ""), res.get("work_phone_source", ""),
            res.get("personal_email", ""), res.get("personal_phone", ""),
            res.get("notes", ""),
        ]
    return base + [
        res.get("personal_email", ""), res.get("personal_email_source", ""),
        res.get("personal_phone", ""), res.get("personal_phone_source", ""),
        res.get("work_email", ""), res.get("work_email_source", ""),
        res.get("work_phone", ""), res.get("work_phone_source", ""),
        res.get("notes", ""),
    ]


def build_workbook(enriched_rows):
    """enriched_rows: list of (candidate_dict, result_dict). Returns bytes."""
    wb = Workbook()
    first = True
    for sheet_name, headers in SHEETS.items():
        ws = wb.active if first else wb.create_sheet(title=sheet_name)
        if first:
            ws.title = sheet_name
            first = False
        ws.append(headers)
        for cand, res in enriched_rows:
            ws.append(_row_for_sheet(sheet_name, cand, res))
        src_col = None
        for idx, h in enumerate(headers, start=1):
            if "Source" in h and "Work" in h:
                src_col = idx
                break
        email_col = None
        for idx, h in enumerate(headers, start=1):
            if h == "Work Email":
                email_col = idx
                break
        for row in ws.iter_rows(min_row=2):
            if src_col:
                _mark_unverified(row[src_col - 1])
            if email_col and src_col:
                src_val = str(row[src_col - 1].value or "")
                if "UNVERIFIED" in src_val.upper():
                    row[email_col - 1].fill = copy(UNVERIFIED_FILL)
        widths = {"A": 14, "B": 14, "C": 26, "D": 28, "E": 34}
        for col_letter, w in widths.items():
            ws.column_dimensions[col_letter].width = w
        for col in range(6, len(headers) + 1):
            ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = 30
        _style_header(ws, len(headers))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def save_workbook(enriched_rows, path: str):
    with open(path, "wb") as f:
        f.write(build_workbook(enriched_rows))
    return path
