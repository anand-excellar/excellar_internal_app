"""Push the per-xltoken NAV catalog report to a Google Sheet in Drive.

Auth reuses the same OAuth client (``credentialsNAV.json``) as the repo-root
``buildNAVSummary.py`` / ``moveNavFilestoDrive.py`` scripts, and by default the
same cached token (``token_summary.pickle``) — which already carries the Drive +
Sheets scopes and a refresh token, so scheduled/headless runs refresh silently
without any interactive step.

Scopes: ``drive.file`` + ``spreadsheets``. Because the folder and the report
sheet are all created by this same OAuth client, ``drive.file`` can see and
update them (it grants access to app-created files).

One spreadsheet per ET calendar date, named "<sheet_title> YYYY-MM-DD" (e.g.
"NAV Catalog Report 2026-07-29"): the first run of a day creates it, later runs
that same day rewrite it in place, and a new date gets its own fresh sheet.

Layout of each day's spreadsheet:
  * "NAV Summary" — that day's rows: one per segment plus a TOTAL. Re-running the
    same day updates those rows in place.
  * "Detail"      — overwritten each run: the full current catalog breakdown
    (Native reserve / Aave / Spark / Exchange & perps) for every xltoken.

Spreadsheet ids are cached per date in a JSON map keyed by date
(settings.NAV_REPORT["sheet_id_file"]); a lost cache is recovered by looking the
day's sheet up by name in the report folder before creating a new one.
"""
import json
import logging
import os
import pickle
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

ET = ZoneInfo("America/New_York")

SUMMARY_TAB = "NAV Summary"
DETAIL_TAB = "Detail"

SUMMARY_HEADER = [
    "Date", "Time (ET)", "Segment", "Base",
    "NAV (native)", "NAV (USD)", "Gross (USD)", "Aave loan (USD)",
    "Native reserve (USD)", "Aave collateral (USD)", "Spark sUSDS (USD)",
    "Exchange equity (USD)", "Snapshot (UTC)",
]
DETAIL_HEADER = [
    "Segment", "Category", "Location (where)", "Item", "Details",
    "Qty", "Price (USD)", "Value (USD)",
]
DETAIL_COLS = len(DETAIL_HEADER)

# Appended to the Detail tab only on a --reconcile run. "NAV fund's" figures are
# NAV Fund Services' own: Qty in the ticker's native units, Value in the fund's
# base currency (BTC/ETH/USDC). Diff (USD) puts both sides on one scale so a
# single threshold works across funds.
RECON_HEADER = [
    "NAV fund's Qty", "NAV fund's NAV (Base)", "Diff (Qty)", "Diff (USD)", "Flag",
]
RECON_COLS = len(RECON_HEADER)

# Spreadsheet mime type used when creating the report file inside the folder.
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def get_credentials(interactive: bool = True):
    """Load cached Google credentials, refreshing or (if allowed) re-authing.

    ``interactive=False`` (scheduled runs) will refresh an expired token via its
    refresh token but never open a browser — it raises instead, so a headless
    run fails loudly rather than hanging on an OAuth prompt.
    """
    from google.auth.transport.requests import Request

    cfg = settings.NAV_REPORT
    token_file = cfg["token_file"]
    creds_file = cfg["credentials_file"]

    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not os.path.exists(creds_file):
            raise RuntimeError(f"Google OAuth client file not found: {creds_file}")
        flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise RuntimeError(
            "No valid Google token and interactive auth is disabled. Run "
            "`python manage.py export_nav_report --authorize` once (interactively) "
            f"to create {token_file}, or point NAV_REPORT['token_file'] at an "
            "existing token that has the Drive + Sheets scopes."
        )

    with open(token_file, "wb") as f:
        pickle.dump(creds, f)
    return creds


# --------------------------------------------------------------------------- #
# Drive / Sheets plumbing
# --------------------------------------------------------------------------- #
def _get_or_create_folder(drive) -> str:
    name = settings.NAV_REPORT["drive_folder_name"]
    query = (
        f"name = '{name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    resp = drive.files().list(q=query, spaces="drive", fields="files(id)").execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    logger.info("Created Drive folder %r", name)
    return folder["id"]


def _sheet_title_for_date(date_et: str) -> str:
    """Per-day spreadsheet name, e.g. 'NAV Catalog Report 2026-07-29'."""
    return f"{settings.NAV_REPORT['sheet_title']} {date_et}"


def _load_id_map() -> dict:
    """Read the {date: spreadsheet_id} cache (tolerant of a missing/legacy file).

    Pre-per-day versions stored a single bare id here; that isn't valid JSON, so
    we treat it as an empty map and simply start writing the dated map going
    forward (the old single sheet is left untouched in Drive).
    """
    id_file = settings.NAV_REPORT["sheet_id_file"]
    if not os.path.exists(id_file):
        return {}
    try:
        with open(id_file) as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_id_map(id_map: dict) -> None:
    id_file = settings.NAV_REPORT["sheet_id_file"]
    try:
        with open(id_file, "w") as f:
            json.dump(id_map, f, indent=2)
    except OSError as exc:
        logger.warning("Could not persist report sheet id cache: %s", exc)


def _get_or_create_sheet(drive, sheets, folder_id: str, date_et: str) -> str:
    """Return the spreadsheet id for ``date_et``, creating it in the folder if new.

    One spreadsheet per ET calendar date: re-running the same day reuses (and
    rewrites) that day's sheet, while a new date gets its own fresh spreadsheet.
    Resolution order: the per-date id cache, then a Drive name lookup (so a lost
    cache still finds an already-created day), then create.
    """
    title = _sheet_title_for_date(date_et)
    id_map = _load_id_map()

    # 1) Cached id for this exact date still resolves → reuse it.
    cached = id_map.get(date_et)
    if cached:
        try:
            sheets.spreadsheets().get(spreadsheetId=cached).execute()
            return cached
        except Exception:
            logger.warning("Cached report sheet %s for %s missing — relocating", cached, date_et)

    # 2) Find this day's sheet by name in the folder (app-created files are
    #    visible to the drive.file scope). Guards against a wiped/legacy cache.
    query = (
        f"name = '{title}' and mimeType = '{_SHEET_MIME}' "
        f"and '{folder_id}' in parents and trashed = false"
    )
    resp = drive.files().list(q=query, spaces="drive", fields="files(id)").execute()
    files = resp.get("files", [])
    if files:
        sheet_id = files[0]["id"]
    else:
        # 3) No sheet for this date yet → create one.
        created = drive.files().create(
            body={"name": title, "mimeType": _SHEET_MIME, "parents": [folder_id]},
            fields="id",
        ).execute()
        sheet_id = created["id"]
        logger.info("Created report spreadsheet %s for %s", sheet_id, date_et)

    id_map[date_et] = sheet_id
    _save_id_map(id_map)
    return sheet_id


def _ensure_tabs(sheets, sheet_id: str, wanted: list[str]) -> None:
    """Make sure each wanted tab exists; drop the default 'Sheet1' if unused."""
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    requests = [
        {"addSheet": {"properties": {"title": t}}}
        for t in wanted if t not in existing
    ]
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()

    # Remove the auto-created "Sheet1" if it's not one we want and other tabs exist.
    if "Sheet1" in existing and "Sheet1" not in wanted:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": existing["Sheet1"]}}]},
        ).execute()


def _tab_meta(sheets, sheet_id: str, tab: str) -> tuple[int | None, int]:
    """``(numeric tab id, how many conditional-format rules it already has)``.

    Formatting requests address tabs by id, not title; the rule count is needed
    because rules must be deleted before being re-added or every run stacks
    another copy.
    """
    meta = sheets.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title),conditionalFormats)").execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"], len(s.get("conditionalFormats") or [])
    return None, 0


def _format_tab(sheets, sheet_id: str, tab: str, *, roles=None,
                ncols: int = 0, nrows: int = 0) -> None:
    """Apply visual formatting to one tab. Never fatal — see ``sheet_format.apply``."""
    from tracker.services import sheet_format

    tab_id, existing = _tab_meta(sheets, sheet_id, tab)
    if tab_id is None:
        return
    if roles is not None:
        requests = sheet_format.detail_requests(tab_id, roles, ncols, DETAIL_COLS, existing)
    else:
        requests = sheet_format.summary_requests(tab_id, nrows, ncols, existing)
    sheet_format.apply(sheets, sheet_id, requests)


def _get_values(sheets, sheet_id: str, tab: str) -> list[list]:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab,
    ).execute()
    return resp.get("values", [])


def _overwrite_tab(sheets, sheet_id: str, tab: str, values: list[list]) -> None:
    sheets.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=tab, body={},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


# --------------------------------------------------------------------------- #
# Value formatting
# --------------------------------------------------------------------------- #
def _num(value):
    """A float for the sheet, or '' for None (so blanks stay blank, not 0)."""
    if value is None:
        return ""
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    try:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _date_ord(d: str) -> int:
    try:
        return datetime.strptime(d, "%Y-%m-%d").toordinal()
    except (ValueError, TypeError):
        return 0


def _money_to_num(text):
    """Parse a formatted '$1,234.56' back to a float (or '' if not parseable)."""
    try:
        return float(str(text).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return ""


def _fmt0(value):
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# Sheet writers
# --------------------------------------------------------------------------- #
def _write_summary(sheets, sheet_id: str, report: dict, date_et: str, time_et: str) -> None:
    """Upsert today's rows into the running NAV Summary history."""
    existing = _get_values(sheets, sheet_id, SUMMARY_TAB)
    # Key existing rows by (date, segment) so a same-day re-run overwrites them.
    rows_by_key: dict[tuple, list] = {}
    for r in existing[1:] if existing else []:
        if len(r) >= 3 and r[0] and r[2]:
            rows_by_key[(r[0], r[2])] = r

    for s in report["segments"]:
        seg = s["segment"]
        if not s.get("has_data"):
            row = [date_et, time_et, seg, s.get("base_asset", ""),
                   "", "", "", "", "", "", "", "", "no data"]
        else:
            c = s["components"]
            row = [
                date_et, time_et, seg, s.get("base_asset", ""),
                _num(s.get("net_native")), _num(s.get("net_usd")),
                _num(s.get("gross_usd")), _num(s.get("aave_loan_usd")),
                _num(c["native_reserve_usd"]), _num(c["aave_collateral_usd"]),
                _num(c["spark_usd"]), _num(c["exchange_equity_usd"]),
                _fmt_ts(s.get("timestamp")),
            ]
        rows_by_key[(date_et, seg)] = row

    rows_by_key[(date_et, "TOTAL")] = [
        date_et, time_et, "TOTAL", "", "", _num(report["total_nav_usd"]),
        "", "", "", "", "", "", "",
    ]

    # Newest date first; within a date, segments A–Z then TOTAL last.
    ordered = sorted(
        rows_by_key.values(),
        key=lambda r: (-_date_ord(r[0]), r[2] == "TOTAL", r[2]),
    )
    _overwrite_tab(sheets, sheet_id, SUMMARY_TAB, [SUMMARY_HEADER] + ordered)
    _format_tab(sheets, sheet_id, SUMMARY_TAB,
                nrows=len(ordered) + 1, ncols=len(SUMMARY_HEADER))


def _blank_detail_row() -> list:
    return [""] * DETAIL_COLS


def _recon_cells(recon_lookup: dict, category: str, location: str, asset: str) -> list:
    """The five reconciliation cells for one catalog row, or blanks.

    Matching is delegated to ``navfund.recon`` so the sheet and the console
    reconciliation can never disagree about which rows correspond.
    """
    from navfund.recon import _key

    row = recon_lookup.get(_key(category, location, asset))
    if row is None:
        return [""] * RECON_COLS
    flag = "BREAK" if row["flagged"] else (f"expected — {row['note']}"
                                          if row["structural"] else "")
    return [_num(row["nav_qty"]), _num(row["nav_value_base"]),
            _num(row["diff_qty"]), _num(row["diff_usd"]), flag]


def _nav_only_rows(seg: str, result: dict) -> list[list]:
    """Rows NAV reports that our catalog has no counterpart for.

    Appended so a position NAV knows about and we do not can never be lost by
    virtue of having nowhere to sit — that is the most valuable thing the
    reconciliation can surface.
    """
    from navfund.recon import ONLY_NAV

    rows = []
    for row in result.get("rows", []):
        if row["presence"] != ONLY_NAV:
            continue
        # NAV emits a zero row for every ticker an account has ever held, plus a
        # blank-ticker placeholder per account. Those aren't positions we're
        # missing, so listing them buries the ones that matter.
        if not row["flagged"] and not row["nav_qty"] and not row["nav_value_base"]:
            continue
        out = _blank_detail_row() + [""] * RECON_COLS
        out[0], out[1], out[2], out[3] = seg, row["category"], row["location"], row["asset"]
        out[4] = "NAV only — no counterpart in our catalog"
        out[DETAIL_COLS], out[DETAIL_COLS + 1] = _num(row["nav_qty"]), _num(row["nav_value_base"])
        out[DETAIL_COLS + 3] = _num(row["diff_usd"])
        out[DETAIL_COLS + 4] = "BREAK" if row["flagged"] else ""
        rows.append(("navonly", out))

    for row in result.get("unmapped", []):
        out = _blank_detail_row() + [""] * RECON_COLS
        out[0], out[2], out[3] = seg, "(unmapped NAV account)", row["nav_account"]
        out[4] = row.get("note") or f"ticker {row['ticker'] or '(none)'} — not in account_map.yaml"
        out[DETAIL_COLS], out[DETAIL_COLS + 1] = _num(row["qty"]), _num(row["value_base"])
        rows.append(("unmapped", out))
    return rows


def _detail_rows_for_segment(seg: str, s: dict, recon_result: dict | None = None,
                             with_roles: bool = False) -> list:
    """Flatten one segment's catalog into fully self-describing rows.

    Every row names the Category (Native reserve / Aave / Spark / Exchange), the
    Location (which custody wallet · network, which venue, which chain) and the
    Item, so "how much and where" is answerable from a single row even after the
    sheet is sorted or filtered. Mirrors the on-screen catalog 1:1, including
    perp side/size/leverage + liquidation buffer and Aave LTV/health factor.

    ``with_roles`` returns ``(role, cells)`` pairs instead of bare rows. The role
    is what ``sheet_format`` styles on — deriving it back from cell contents
    afterwards would be guesswork, so it is recorded as each row is built.
    """
    tagged: list[tuple[str, list]] = []

    def emit(role: str, cells: list) -> None:
        tagged.append((role, cells))

    def result():
        # Every row in a segment must be the same width or the sheet's columns
        # shear; banner and summary rows are built at DETAIL_COLS.
        if recon_result is not None:
            width = DETAIL_COLS + RECON_COLS
            for _, cells in tagged:
                cells.extend([""] * (width - len(cells)))
        return tagged if with_roles else [cells for _, cells in tagged]

    if not s.get("has_data"):
        r = _blank_detail_row()
        r[0], r[3] = seg, "no data"
        emit("asset", r)
        return result()

    recon_lookup = None
    if recon_result is not None and not recon_result.get("error"):
        from navfund import recon as _recon

        recon_lookup = _recon.lookup(recon_result)

    for section in s.get("sections", []):
        cat = section.get("title", "")
        note = section.get("note") or ""
        # A light category banner carries the section note (e.g. "health factor
        # 1.85", "2 custody wallets"); every data row below is still self-describing.
        banner = _blank_detail_row()
        banner[0], banner[1], banner[4] = seg, cat, note
        emit("banner", banner)

        location = ""  # carried down from the current wallet/venue header
        is_exchange = cat.lower().startswith("exchange")
        is_aave = cat.lower().startswith("aave")
        is_spark = cat.lower().startswith("spark")

        for r in section.get("rows", []):
            if r.get("wallet_header"):
                name = r.get("wallet_header", "")
                wn = str(r.get("wallet_net") or "")
                if is_exchange:
                    # Venue header: wallet_net is the venue's "$equity".
                    location = name
                    emit("wallet", [seg, cat, location, "Account equity",
                                    "enters NAV", "", "", _money_to_num(wn)])
                else:
                    # Custody wallet header: wallet_net is the wallet's coin/network.
                    location = f"{name} · {wn}" if wn else name
                    emit("wallet", [seg, cat, location, "Custody wallet",
                                    f"network {wn}" if wn else "", "", "", ""])
            elif r.get("total_row") or r.get("kv_row"):
                emit("summary", [seg, cat, location, r.get("label", ""),
                                 "", "", "", _num(r.get("value"))])
            elif r.get("perp"):
                details = r.get("sub", "")
                if r.get("liq_pct") is not None:
                    details += (f" · liq ${_fmt0(r.get('liq_price'))} "
                                f"({_fmt0(r.get('liq_pct'))}% buffer)")
                emit("asset", [seg, cat, location, f"{r.get('asset', '')} (uPnL)",
                               details, "", "", _num(r.get("value"))])
            else:
                # Standard asset / Aave collateral·borrow / Spark row.
                sub = r.get("sub", "")
                if is_aave:
                    loc = "Aave"
                elif is_spark:
                    loc = sub.split("·")[0].strip() if sub else "Spark"
                else:
                    loc = location
                asset = r.get("asset", "")
                cells = [seg, cat, loc, asset, sub,
                         _num(r.get("qty")), _num(r.get("price")), _num(r.get("value"))]
                if recon_lookup is not None:
                    cells += _recon_cells(recon_lookup, cat, loc, asset)
                emit("asset", cells)

    # Per-segment NAV footer.
    net_native = s.get("net_native")
    native_str = (f"{net_native:.6f} {s.get('base_asset', '')}"
                  if net_native is not None else "")
    footer = _blank_detail_row()
    footer[0], footer[1], footer[3], footer[7] = (
        seg, "NET NAV", native_str, _num(s.get("net_usd")))
    if s.get("aave_loan_usd"):
        footer[4] = (f"gross ${_fmt0(s.get('gross_usd'))} − "
                     f"${_fmt0(s.get('aave_loan_usd'))} Aave loan")
    if recon_result is not None:
        footer += [""] * RECON_COLS
        if not recon_result.get("error"):
            footer[DETAIL_COLS + 1] = _num(recon_result.get("nav_total_base"))
            footer[DETAIL_COLS + 3] = _num(recon_result.get("diff_total_usd"))
            breaks = recon_result.get("flagged_count") or 0
            footer[DETAIL_COLS + 4] = f"{breaks} break(s) over ${recon_result['threshold_usd']:g}"
        else:
            footer[DETAIL_COLS + 4] = f"recon failed: {recon_result['error'][:120]}"
    emit("footer", footer)

    if recon_result is not None and not recon_result.get("error"):
        for role, cells in _nav_only_rows(seg, recon_result):
            emit(role, cells)
    return result()


def _write_detail(sheets, sheet_id: str, report: dict, time_et_full: str,
                  recon: dict | None = None) -> None:
    gen = _blank_detail_row()
    gen[0] = f"Generated {time_et_full} ET"
    header = list(DETAIL_HEADER)
    if recon is not None:
        header += RECON_HEADER
        dates = sorted({r.get("report_date") for r in recon.values() if r.get("report_date")})
        gen += [""] * RECON_COLS
        gen[DETAIL_COLS] = f"NAV fund data as of {', '.join(map(str, dates))}" if dates else ""

    values = [gen, header]
    roles = ["stamp", "header"]
    for s in report["segments"]:
        seg_recon = recon.get(s["segment"]) if recon is not None else None
        for role, cells in _detail_rows_for_segment(s["segment"], s, seg_recon,
                                                    with_roles=True):
            values.append(cells)
            roles.append(role)
        values.append(_blank_detail_row())  # spacer between segments
        roles.append("spacer")

    total = _blank_detail_row()
    total[0], total[7] = "TOTAL NAV (all xltokens)", _num(report["total_nav_usd"])
    if recon is not None:
        total += [""] * RECON_COLS
        total[DETAIL_COLS + 3] = _num(sum(r.get("diff_total_usd") or 0.0
                                          for r in recon.values()))
    values.append(total)
    roles.append("total")

    _overwrite_tab(sheets, sheet_id, DETAIL_TAB, values)
    _format_tab(sheets, sheet_id, DETAIL_TAB, roles=roles, ncols=len(header))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def export_report_to_drive(report: dict, interactive: bool = True,
                           recon: dict | None = None, date_et: str | None = None) -> str:
    """Push ``report`` (from nav_report.build_report) to the Google Sheet.

    Returns the spreadsheet URL. ``interactive=False`` for scheduled/headless
    runs (refresh-only auth, never opens a browser).

    ``recon`` (from ``navfund.recon.reconcile_report``) adds the NAV-fund columns.
    ``date_et`` names the day's spreadsheet to write into; it must be passed when
    rebuilding a past date, since deriving it from ``generated_at`` would put a
    back-dated reconciliation into today's sheet.
    """
    from googleapiclient.discovery import build

    creds = get_credentials(interactive=interactive)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    generated = report["generated_at"].astimezone(ET)
    date_et = date_et or generated.strftime("%Y-%m-%d")
    time_et = generated.strftime("%H:%M")
    time_et_full = generated.strftime("%Y-%m-%d %H:%M")

    folder_id = _get_or_create_folder(drive)
    sheet_id = _get_or_create_sheet(drive, sheets, folder_id, date_et)
    _ensure_tabs(sheets, sheet_id, [SUMMARY_TAB, DETAIL_TAB])
    _write_summary(sheets, sheet_id, report, date_et, time_et)
    _write_detail(sheets, sheet_id, report, time_et_full, recon)

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    logger.info("NAV catalog report written: %s", url)
    return url