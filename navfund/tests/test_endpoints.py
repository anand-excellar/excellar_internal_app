"""Date handling and balance-sheet extraction, against real response shapes.

Payloads here are trimmed copies of live responses (see `../docs/nav_api_reference.md`).
"""
from datetime import date, datetime

from navfund import endpoints as ep


class FakeClient:
    """Records the params it was called with and replays a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append((endpoint, params))
        return self.payload


# Trimmed from a live XLETH SA response — note the fund-varying tag set (XLBTC
# has no Short Portfolio rows) and the {"DTD": value} period wrapper.
BALANCE_SHEET = {
    "Data": [
        {"date": "08-08-2026",
         "Ending Net Asset Value": {"DTD": 3.057145},
         "Ending Cash Balance": {"DTD": 2.810211}},
        {"date": "08-09-2026",
         "Ending Net Asset Value": {"DTD": 3.057101},
         "Short Portfolio Value- Market Value": {"DTD": -0.030736}},
    ],
    "TagsInfo": [
        {"Ending Net Asset Value": {"Name": "Ending Net Asset Value", "GLCode": 70031,
                                    "Category": "Capital"}},
    ],
}


def test_request_dates_are_mm_dd_yyyy():
    assert ep.nav_date(date(2026, 8, 9)) == "08-09-2026"
    assert ep.nav_date(datetime(2026, 8, 9, 13, 45)) == "08-09-2026"
    # Round-trips an ISO response value back into request format.
    assert ep.nav_date("2026-08-09T00:00:00Z") == "08-09-2026"


def test_response_dates_parse_in_both_flavours():
    assert ep.parse_nav_date("2026-08-09T00:00:00Z") == date(2026, 8, 9)
    assert ep.parse_nav_date("2026-08-09T00:00:00") == date(2026, 8, 9)
    assert ep.parse_nav_date("08-09-2026") == date(2026, 8, 9)
    assert ep.parse_nav_date(date(2026, 8, 9)) == date(2026, 8, 9)


def test_response_dates_tolerate_missing_values():
    assert ep.parse_nav_date(None) is None
    assert ep.parse_nav_date("") is None
    assert ep.parse_nav_date("not a date") is None


def test_ending_nav_series_keys_by_date_and_unwraps_dtd():
    client = FakeClient(BALANCE_SHEET)
    series = ep.ending_nav_series(client, 247834, date(2026, 8, 8), date(2026, 8, 9))
    assert series == {date(2026, 8, 8): 3.057145, date(2026, 8, 9): 3.057101}


def test_ending_nav_series_sends_the_documented_params():
    client = FakeClient(BALANCE_SHEET)
    ep.ending_nav_series(client, 247834, date(2026, 8, 1), "2026-08-09T00:00:00Z")
    endpoint, params = client.calls[0]
    assert endpoint == ep.BALANCE_SHEET
    assert params == {"globalFundID": 247834,
                      "fromDate": "08-01-2026", "toDate": "08-09-2026"}


def test_dates_without_a_nav_tag_are_omitted_not_zeroed():
    """A gap in NAV's data must stay a gap — a 0 would look like a real NAV."""
    client = FakeClient({"Data": [
        {"date": "08-08-2026", "Ending Cash Balance": {"DTD": 2.81}},
        {"date": "08-09-2026", "Ending Net Asset Value": {"DTD": 3.057101}},
    ]})
    series = ep.ending_nav_series(client, 247834, date(2026, 8, 8), date(2026, 8, 9))
    assert series == {date(2026, 8, 9): 3.057101}


def test_empty_and_missing_payloads_yield_no_series():
    assert ep.ending_nav_series(FakeClient({}), 1, date(2026, 8, 9), date(2026, 8, 9)) == {}
    assert ep.ending_nav_series(FakeClient({"Data": None}), 1,
                                date(2026, 8, 9), date(2026, 8, 9)) == {}


def test_period_unwrapping_falls_back_to_a_lone_key():
    assert ep._period_value({"DTD": 1.5}) == 1.5
    assert ep._period_value({"MTD": 2.5}) == 2.5      # single key, differently named
    assert ep._period_value({"MTD": 1, "YTD": 2}) is None  # ambiguous — refuse to guess
    assert ep._period_value(0.5) == 0.5


def test_trading_gain_loss_sends_report_date():
    client = FakeClient([])
    ep.get_trading_gain_loss(client, 247833, date(2026, 8, 9))
    endpoint, params = client.calls[0]
    assert endpoint == ep.TRADING_GAIN_LOSS
    assert params == {"globalFundID": 247833, "reportDate": "08-09-2026"}