"""Typed wrappers for the NAV Fund Services endpoints we actually use.

Routes are taken from the OpenAPI document (`docs/swagger.json`) rather than
inferred — the controller segments do not follow the written docs' group names.

Date handling is asymmetric and easy to get wrong: **requests** take MM-DD-YYYY,
**responses** come back either ISO 8601 (`2026-08-09T00:00:00Z`) or MM-DD-YYYY
(the balance sheet's per-row `date`), so both directions go through the helpers
here.
"""
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

FUND_LIST = "/api/v1/ClientMasterData/GetFundList"
BALANCE_SHEET = "/api/v1/FundAccountingData/GetBalanceSheetForFund"
TRADING_GAIN_LOSS = "/api/v1/FundReportData/GetTradingGainLossForFund"
UNREALIZED_TAX_LOT = "/api/v1/FundReportData/GetUnRealizedTaxLotForFund"
PORTFOLIO_DATA_DATES = "/api/v1/ClientMasterData/GetPortfolioDataDates"
ACCOUNTING_DATA_DATES = "/api/v1/ClientMasterData/GetAccountingDataDates"

# The balance-sheet tag carrying the fund's NAV, in the fund's base currency.
ENDING_NAV_TAG = "Ending Net Asset Value"

# Gross portfolio value before liabilities — this, NOT Ending Net Asset Value, is
# what the per-account rows sum to. Verified 08-09-2026 on xlUSD, where the two
# differ by exactly the "Performance/Incentive Fee Payable" of -3.29459780:
#   per-account rows 5263.69552129 == End Computed Equity 5263.695521
#   Ending Net Asset Value 5260.40092320 == that minus the fee
# Reconciling positions against Ending NAV would therefore show every fund's
# accrued fees as a phantom break.
COMPUTED_EQUITY_TAG = "End Computed Equity"

# Each tag's value is wrapped in a period object; the daily figure is DTD.
DEFAULT_PERIOD = "DTD"


def nav_date(value) -> str:
    """A date/datetime/string as the MM-DD-YYYY that requests require."""
    if isinstance(value, str):
        return nav_date(parse_nav_date(value))
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%m-%d-%Y")


def parse_nav_date(text) -> date | None:
    """Parse either response flavour: ISO 8601, or MM-DD-YYYY."""
    if text is None or isinstance(text, date) and not isinstance(text, datetime):
        return text
    if isinstance(text, datetime):
        return text.date()
    raw = str(text).strip()
    if not raw:
        return None
    try:  # ISO 8601, with or without the Z
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%m-%d-%Y").date()
    except ValueError:
        logger.warning("Unparseable NAV date %r", text)
        return None


def _period_value(tag_obj, period: str = DEFAULT_PERIOD):
    """Unwrap ``{"DTD": 0.05}``, tolerating a differently named single period."""
    if not isinstance(tag_obj, dict):
        return tag_obj
    if period in tag_obj:
        return tag_obj[period]
    return next(iter(tag_obj.values())) if len(tag_obj) == 1 else None


# --------------------------------------------------------------------------- #
# Master data
# --------------------------------------------------------------------------- #
def get_fund_list(client) -> list[dict]:
    """Every fund on the tenant, with its data-availability gates.

    Fields used downstream: ``FundName``, ``GlobalFundID``,
    ``FundDailyAccountingStartDate``, ``FundDailyAccountingLastAvailableDate``,
    ``PortfolioLastAvailableDate`` (null ⇒ portfolio not set up for that fund).
    """
    return client.get(FUND_LIST) or []


# --------------------------------------------------------------------------- #
# Accounting data
# --------------------------------------------------------------------------- #
def get_balance_sheet(client, global_fund_id: int, from_date, to_date) -> dict:
    """Raw balance sheet: ``{"Data": [{date, tag: {DTD: v}, ...}], "TagsInfo": [...]}``.

    One ``Data`` row per calendar date in the range, weekends included. All values
    are in the fund's base currency (BTC for XLBTC SA, ETH for XLETH SA, …).
    """
    return client.get(BALANCE_SHEET, {
        "globalFundID": global_fund_id,
        "fromDate": nav_date(from_date),
        "toDate": nav_date(to_date),
    }) or {}


def tag_series(client, global_fund_id: int, from_date, to_date, tag: str) -> dict:
    """``{date: value}`` for one balance-sheet tag over the range, in base currency.

    Dates whose row lacks the tag are omitted rather than reported as zero, so a
    gap in NAV's data stays visibly a gap instead of looking like a real zero.
    """
    payload = get_balance_sheet(client, global_fund_id, from_date, to_date)
    series = {}
    for row in payload.get("Data") or []:
        day = parse_nav_date(row.get("date"))
        value = _period_value(row.get(tag))
        if day is not None and value is not None:
            series[day] = value
    return series


def ending_nav_series(client, global_fund_id: int, from_date, to_date) -> dict:
    """``{date: Ending Net Asset Value}`` — the fund's NAV, after liabilities."""
    return tag_series(client, global_fund_id, from_date, to_date, ENDING_NAV_TAG)


def computed_equity_series(client, global_fund_id: int, from_date, to_date) -> dict:
    """``{date: End Computed Equity}`` — the figure per-account rows tie to.

    Use this for position reconciliation; see ``COMPUTED_EQUITY_TAG``.
    """
    return tag_series(client, global_fund_id, from_date, to_date, COMPUTED_EQUITY_TAG)


# --------------------------------------------------------------------------- #
# Portfolio / report data
# --------------------------------------------------------------------------- #
def get_trading_gain_loss(client, global_fund_id: int, report_date) -> list[dict]:
    """One row per (account, ticker) — the source for per-account reconciliation.

    Maps onto their spreadsheet's "Portfolio Valuation By Account" tab:
    ``AccName`` / ``Ticker`` / ``SecurityType`` / ``Quantity`` (Position) /
    ``PriceBase`` (Settlement Price) / ``MarketValueBase`` (Market Value (Base)).
    """
    return client.get(TRADING_GAIN_LOSS, {
        "globalFundID": global_fund_id,
        "reportDate": nav_date(report_date),
    }) or []
