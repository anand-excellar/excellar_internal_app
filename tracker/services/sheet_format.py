"""Visual formatting for the NAV report spreadsheet.

Kept apart from ``gsheets.py`` so the writer stays about *what* the numbers are
and this stays about how they read. Both tabs are rewritten from scratch on every
run, so formatting is re-applied every run too and must be idempotent — hence the
conditional-format rules are deleted before being re-added, or they would pile up
into hundreds of duplicates over a few weeks.

Design rules this follows:

* **Structure comes from weight and space, colour only confirms it.** Section
  banners, wallet rows and footers are distinguishable in greyscale; the tints are
  there to make scanning faster, not to carry meaning on their own.
* **Colour never carries meaning alone.** A break is red *and* says "BREAK", so
  it survives printing, colour-blindness and someone's dark theme.
* **One accent hue per role.** Navy for structure, amber for totals, red only for
  breaks, orange for "NAV knows about this and we don't". Nothing else is
  coloured, so a red cell always means the same thing.
* **Numbers are right-aligned with fixed decimals** so magnitudes line up
  vertically and a stray 1e-08 can't masquerade as 1.
"""
import logging

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #
NAVY = "1F3864"          # header / grand total — the report's anchor colour
NAVY_TEXT = "10243E"     # readable navy for text on light tints
BANNER = "D9E2F3"        # category banner (Native reserve, Aave position, …)
WALLET = "EFF3F9"        # a wallet or venue's own row
FOOTER = "FFF2CC"        # per-segment NET NAV
NAV_ONLY = "FCE4D6"      # NAV reports it, we have no counterpart
BREAK_BG = "FFC7CE"      # difference over the threshold
BREAK_TEXT = "9C0006"
MUTED = "7F7F7F"         # "expected" annotations, the generated-at stamp
GRID = "D0D7E5"
WHITE = "FFFFFF"

QTY_FORMAT = "#,##0.00000000"
USD_FORMAT = "$#,##0.00;[Red]-$#,##0.00"
PRICE_FORMAT = "$#,##0.00"

# Column widths, in pixels, for the eight base columns then the five recon ones.
DETAIL_WIDTHS = [62, 168, 214, 132, 210, 132, 88, 104, 124, 138, 116, 96, 260]
SUMMARY_WIDTH_FIRST = [86, 76, 74, 58]


def _rgb(hex_colour: str) -> dict:
    h = hex_colour.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def _col_letter(index: int) -> str:
    """0-based column index → A1 letter, for conditional-format formulas."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _range(tab_id: int, r0=None, r1=None, c0=None, c1=None) -> dict:
    out = {"sheetId": tab_id}
    if r0 is not None:
        out["startRowIndex"] = r0
    if r1 is not None:
        out["endRowIndex"] = r1
    if c0 is not None:
        out["startColumnIndex"] = c0
    if c1 is not None:
        out["endColumnIndex"] = c1
    return out


def _repeat(tab_id, fmt: dict, fields: str, **bounds) -> dict:
    return {"repeatCell": {"range": _range(tab_id, **bounds),
                           "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _text(bold=False, italic=False, colour=None, size=10) -> dict:
    out = {"bold": bold, "italic": italic, "fontSize": size, "fontFamily": "Inter"}
    if colour:
        out["foregroundColor"] = _rgb(colour)
    return out


def _runs(roles: list, wanted: set):
    """Consecutive row spans sharing a role, so one request covers many rows."""
    start = None
    for i, role in enumerate(roles + [None]):
        if role in wanted and start is None:
            start = i
        elif role not in wanted and start is not None:
            yield start, i
            start = None


# --------------------------------------------------------------------------- #
# Detail tab
# --------------------------------------------------------------------------- #
def detail_requests(tab_id: int, roles: list, ncols: int, base_cols: int,
                    existing_rules: int = 0) -> list:
    """Formatting for the Detail tab, given each row's role.

    ``roles`` is parallel to the written rows: "stamp", "header", "banner",
    "wallet", "asset", "summary", "footer", "navonly", "unmapped", "total",
    "spacer".
    """
    nrows = len(roles)
    has_recon = ncols > base_cols
    req = []

    # Drop previous conditional rules (highest index first) so re-runs don't stack.
    for i in range(existing_rules - 1, -1, -1):
        req.append({"deleteConditionalFormatRule": {"sheetId": tab_id, "index": i}})

    # Header stays visible, and so do the columns that say what a row *is*.
    req.append({"updateSheetProperties": {
        "properties": {"sheetId": tab_id,
                       "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 4}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})

    # Baseline: plain, tight, clipped rather than wrapped so rows stay one line.
    req.append(_repeat(tab_id,
                       {"backgroundColor": _rgb(WHITE), "textFormat": _text(),
                        "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                        "horizontalAlignment": "LEFT"},
                       "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,"
                       "wrapStrategy,horizontalAlignment)",
                       r0=0, r1=nrows, c0=0, c1=ncols))

    # Generated-at stamp.
    req.append(_repeat(tab_id, {"textFormat": _text(italic=True, colour=MUTED, size=9)},
                       "userEnteredFormat.textFormat", r0=0, r1=1, c0=0, c1=ncols))

    # Header row.
    req.append(_repeat(tab_id,
                       {"backgroundColor": _rgb(NAVY),
                        "textFormat": _text(bold=True, colour=WHITE),
                        "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"},
                       "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,"
                       "wrapStrategy)", r0=1, r1=2, c0=0, c1=ncols))
    req.append({"updateDimensionProperties": {
        "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
        "properties": {"pixelSize": 40}, "fields": "pixelSize"}})

    # Row roles. Each is legible without colour: banners and footers are bold,
    # NAV-only rows italic.
    role_styles = [
        ({"banner"}, {"backgroundColor": _rgb(BANNER),
                      "textFormat": _text(bold=True, colour=NAVY_TEXT)}),
        ({"wallet"}, {"backgroundColor": _rgb(WALLET),
                      "textFormat": _text(bold=True, colour=NAVY_TEXT, size=9)}),
        ({"summary"}, {"textFormat": _text(italic=True, colour=MUTED)}),
        ({"footer"}, {"backgroundColor": _rgb(FOOTER), "textFormat": _text(bold=True)}),
        ({"navonly", "unmapped"}, {"backgroundColor": _rgb(NAV_ONLY),
                                   "textFormat": _text(italic=True)}),
        ({"total"}, {"backgroundColor": _rgb(NAVY),
                     "textFormat": _text(bold=True, colour=WHITE, size=11)}),
    ]
    for wanted, fmt in role_styles:
        for r0, r1 in _runs(roles, wanted):
            req.append(_repeat(tab_id, fmt,
                               "userEnteredFormat(backgroundColor,textFormat)",
                               r0=r0, r1=r1, c0=0, c1=ncols))

    # A line above each segment footer and the grand total, to close the block.
    for wanted in ({"footer"}, {"total"}):
        for r0, _ in _runs(roles, wanted):
            req.append(_repeat(tab_id,
                               {"borders": {"top": {"style": "SOLID", "color": _rgb(NAVY)}}},
                               "userEnteredFormat.borders",
                               r0=r0, r1=r0 + 1, c0=0, c1=ncols))

    # Numbers: right-aligned, fixed decimals, so columns compare vertically.
    numeric = [(5, QTY_FORMAT), (6, PRICE_FORMAT), (7, USD_FORMAT)]
    if has_recon:
        numeric += [(base_cols + 0, QTY_FORMAT), (base_cols + 1, QTY_FORMAT),
                    (base_cols + 2, QTY_FORMAT), (base_cols + 3, USD_FORMAT)]
    for col, fmt in numeric:
        req.append(_repeat(tab_id,
                           {"numberFormat": {"type": "NUMBER", "pattern": fmt},
                            "horizontalAlignment": "RIGHT"},
                           "userEnteredFormat(numberFormat,horizontalAlignment)",
                           r0=2, r1=nrows, c0=col, c1=col + 1))

    for col, width in enumerate(DETAIL_WIDTHS[:ncols]):
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                      "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})

    if has_recon:
        # A rule rather than a tint: marks where our figures end and NAV's begin
        # without fighting the row backgrounds.
        req.append(_repeat(tab_id,
                           {"borders": {"left": {"style": "SOLID_MEDIUM",
                                                 "color": _rgb(NAVY)}}},
                           "userEnteredFormat.borders",
                           r0=0, r1=nrows, c0=base_cols, c1=base_cols + 1))

        flag_col = base_cols + 4
        diff_col = base_cols + 3
        diff_letter = _col_letter(diff_col)
        flag_letter = _col_letter(flag_col)

        # Breaks: red fill and dark red bold text across the whole row, on top of
        # whatever its role colour is. The cell also literally reads "BREAK", so
        # the meaning survives printing and colour-blindness.
        req.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [_range(tab_id, r0=2, r1=nrows, c0=0, c1=ncols)],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [
                    {"userEnteredValue": f'=${flag_letter}2="BREAK"'}]},
                "format": {"backgroundColor": _rgb(BREAK_BG),
                           "textFormat": {"bold": True,
                                          "foregroundColor": _rgb(BREAK_TEXT)}}}}}})

        # Known structural offsets: muted, so they read as explained, not wrong.
        req.append({"addConditionalFormatRule": {"index": 1, "rule": {
            "ranges": [_range(tab_id, r0=2, r1=nrows, c0=flag_col, c1=flag_col + 1)],
            "booleanRule": {
                "condition": {"type": "TEXT_STARTS_WITH",
                              "values": [{"userEnteredValue": "expected"}]},
                "format": {"textFormat": {"italic": True,
                                          "foregroundColor": _rgb(MUTED)}}}}}})

        # Sign colouring on the difference itself, independent of the flag.
        req.append({"addConditionalFormatRule": {"index": 2, "rule": {
            "ranges": [_range(tab_id, r0=2, r1=nrows, c0=diff_col, c1=diff_col + 1)],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{
                    "userEnteredValue": (f'=AND(ISNUMBER(${diff_letter}2),'
                                         f'ABS(${diff_letter}2)>1)')}]},
                "format": {"textFormat": {"bold": True,
                                          "foregroundColor": _rgb(BREAK_TEXT)}}}}}})

    req.append({"updateBorders": {
        "range": _range(tab_id, r0=1, r1=nrows, c0=0, c1=ncols),
        "innerHorizontal": {"style": "SOLID", "color": _rgb(GRID)}}})
    return req


# --------------------------------------------------------------------------- #
# Summary tab
# --------------------------------------------------------------------------- #
def summary_requests(tab_id: int, nrows: int, ncols: int,
                     existing_rules: int = 0) -> list:
    """Formatting for the running NAV Summary history."""
    req = []
    for i in range(existing_rules - 1, -1, -1):
        req.append({"deleteConditionalFormatRule": {"sheetId": tab_id, "index": i}})

    req.append({"updateSheetProperties": {
        "properties": {"sheetId": tab_id,
                       "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 3}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})

    req.append(_repeat(tab_id,
                       {"backgroundColor": _rgb(WHITE), "textFormat": _text(),
                        "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP"},
                       "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,"
                       "wrapStrategy)", r0=0, r1=nrows, c0=0, c1=ncols))

    req.append(_repeat(tab_id,
                       {"backgroundColor": _rgb(NAVY),
                        "textFormat": _text(bold=True, colour=WHITE),
                        "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"},
                       "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,"
                       "verticalAlignment)", r0=0, r1=1, c0=0, c1=ncols))
    req.append({"updateDimensionProperties": {
        "range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 46}, "fields": "pixelSize"}})

    # Base-currency NAV needs decimals (0.05 BTC); USD columns do not.
    req.append(_repeat(tab_id,
                       {"numberFormat": {"type": "NUMBER", "pattern": QTY_FORMAT},
                        "horizontalAlignment": "RIGHT"},
                       "userEnteredFormat(numberFormat,horizontalAlignment)",
                       r0=1, r1=nrows, c0=4, c1=5))
    req.append(_repeat(tab_id,
                       {"numberFormat": {"type": "NUMBER", "pattern": USD_FORMAT},
                        "horizontalAlignment": "RIGHT"},
                       "userEnteredFormat(numberFormat,horizontalAlignment)",
                       r0=1, r1=nrows, c0=5, c1=ncols))

    # TOTAL rows carry the eye down the history; tint them rather than bold every
    # cell, so the per-segment rows above stay the default weight.
    req.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [_range(tab_id, r0=1, r1=nrows, c0=0, c1=ncols)],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": '=$C2="TOTAL"'}]},
            "format": {"backgroundColor": _rgb(FOOTER),
                       "textFormat": {"bold": True}}}}}})

    for col, width in enumerate(SUMMARY_WIDTH_FIRST):
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                      "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    req.append({"updateDimensionProperties": {
        "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                  "startIndex": len(SUMMARY_WIDTH_FIRST), "endIndex": ncols},
        "properties": {"pixelSize": 132}, "fields": "pixelSize"}})

    req.append({"updateBorders": {
        "range": _range(tab_id, r0=0, r1=nrows, c0=0, c1=ncols),
        "innerHorizontal": {"style": "SOLID", "color": _rgb(GRID)}}})
    return req


def apply(sheets, spreadsheet_id: str, requests: list) -> None:
    """Send formatting requests, tolerating a partial failure.

    Formatting is cosmetic: if a request is rejected we log it and leave the
    numbers in place rather than failing a report that is otherwise correct.
    """
    if not requests:
        return
    try:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    except Exception as exc:
        logger.warning("Report formatting skipped: %s", exc)