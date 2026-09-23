# `navfund` — NAV Fund Services reconciliation

Brings NAV Fund Services' own daily valuation into the NAV catalog report we
already generate, as `NAV fund's NAV` / `Diff` columns, so their numbers and ours
are reconciled automatically instead of pasted in by hand.

Everything NAV-Fund-Services-specific lives in this folder. Only three small
touch points exist outside it (listed under *Wiring* below).

## Layout

| Path | Purpose | Status |
|---|---|---|
| `client.py` | HMAC-SHA256 signing + signed GET transport | **done, test-pinned** |
| `sign_headers.py` | CLI: print paste-ready headers / curl, or call the API | **done** |
| `tests/test_signing.py` | Reproduces NAV's published auth test vector | **done, 10 passing** |
| `docs/nav_api_reference.md` | Versioned transcription of NAV's API docs | **done** |
| `endpoints.py` | Typed wrappers per endpoint (fund list, cash balances, …) | pending creds |
| `normalize.py` | Raw NAV JSON → flat comparable rows | pending live payload |
| `account_map.yaml` + `mapping.py` | NAV account → our BitGo/venue label | pending live payload |
| `models.py` | `NavFundValuation`, fetch/LogID bookkeeping | pending schema |
| `recon.py` | Join NAV rows to our catalog, compute diffs | pending above |
| `management/commands/navfund_discover.py` | Dump one day's raw JSON per endpoint | pending creds |
| `management/commands/navfund_fetch.py` | Fetch + store a date (range) | pending schema |

## Agreed specification

**Units — no USD conversion anywhere.**

| Row type in the Detail tab | NAV figure used | Ties against |
|---|---|---|
| Per-asset row (USDC, ARBETH, `ETH:AETHWBTC`) | `Position` (native ticker units) | our `Qty` |
| Wallet-label rollup row | `Market Value (Base)`, summed over that wallet's NAV accounts + tickers | our wallet subtotal in base currency |
| Segment `NET NAV` footer | GRAND TOTAL `Market Value (Base)` | our `net_native` |

Base currency is the fund's: BTC for XLBTC SA, ETH for XLETH SA, USDC for XLUSD
SA. Our side's base value is derived as `Value (USD) ÷ _segment_price`, because
the Detail tab has no base-currency column of its own.

Two diffs, because they fail differently: **Diff (Qty)** is price-clean, so a
break there is a genuine position difference; **Diff (Base)** also absorbs NAV's
settlement price differing from our feed.

**Rollup.** NAV splits a wallet across sub-accounts (`…_GAS`, `…_gas`,
`…_REWARD`); those sum into the single parent label we already show. Summing is
only meaningful in base currency — you cannot add a USDC position to an
`ETH_GAS` position in native units, which is why the rollup row uses
`Market Value (Base)` and only per-asset rows carry `Position`.

**Structure is unchanged — aTokens are not rerouted.** NAV reports Aave
collateral as an aToken sitting inside the wallet; we keep aTokens unpriced in
custody and value Aave in its own section. So a wallet line holding aTokens shows
a non-zero diff *by construction*, equal to the collateral. That is annotated in
the Details column, not hidden and not patched. The real tie-outs are the
per-asset quantity rows and the segment `NET NAV` row.

**Scope.** XLBTC SA, XLETH SA, XLUSD SA. `USDXLR SA` is logged if it appears but
never mapped — we have no matching segment.

**Schedule and flags.** NAV publishes at 5 AM ET, so our report moves to 5 AM ET
(`config/config.yaml: report_hour_et`). The daily run keeps today's 8 columns
untouched; recon columns appear only on demand:

```
python manage.py export_nav_report                               # daily, unchanged
python manage.py export_nav_report --reconcile --date 2026-07-30  # + NAV + Diff columns
python manage.py export_nav_report --reconcile                    # date defaults to today
python manage.py export_nav_report --reconcile --date D --dry-run  # print, touch nothing
```

For a past date, our side of the comparison is **the latest snapshot at or before
5 AM ET on that date** — reproducing what that morning's report published, not
diffing NAV against our current intraday position. (`prune_json` only nulls
`raw_debank_json`, so `bitgo_holdings_json` survives and historical reconciles
keep full per-wallet detail.)

Red highlight fires on **either** an absolute threshold (base units) **or** a bps
threshold. No backfill for now, though `navfund_fetch` takes a date range so
enabling it later is an argument, not a redesign.

## How the API maps onto their report

The `Portfolio Valuation By Account` tab we're reconciling against has
`Security Type = CASH` on every row — including the aTokens — so the
corresponding endpoint is **`GetCashBalancesForFund(GlobalFundID, ReportDate)`**,
not a positions endpoint.

| Their report tab | Endpoint |
|---|---|
| Portfolio Valuation By Account | `GetCashBalancesForFund` |
| Portfolio Valuation By Exchange | same data, different grouping |
| Trading Gain Loss | `GetTradingGainLossForFund` |
| Unmapped Cash | expected within `GetCashBalancesForFund` |
| Fund headline NAV | `GetEstimateNAVAndPerformanceReturnsForFund` (daily estimate) |

Non-cash securities, if they ever appear, would come from
`GetUnRealizedTaxLotForFund`.

**Date availability is answered by the API, not guessed.** `GetFundList` returns
`PortfolioLastAvailableDate` per fund; a `--date` beyond it has no data and fails
loudly rather than silently reporting stale figures. `GetPortfolioDataDates`
carries a `LogID` differential cursor, which also tells us when NAV *restates* a
date we already reconciled — worth storing.

## Credentials

Environment only, never source control (`.env`):

```
NAVFUND_API_KEY=...
NAVFUND_API_SECRET=...
NAVFUND_API_HOST=api.navfundservices.com   # optional override
```

Signing is already proven against NAV's published vector, so a 401 in production
means a bad key/secret, a lost `/navapigateway` prefix, or a host clock more than
5 minutes off UTC — not a signing bug.

## Driving the portal's Swagger "Execute" button

Swagger does not sign for you — it sends whatever you paste, and its two
prefilled defaults are both wrong (`x-date` prefills as ISO 8601 when NAV wants
RFC 1123 GMT; `x-content-sha256` prefills as a literal `""`). Generate the real
values:

```
python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList
python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList --curl
python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList --execute
```

Three rules follow from how the signature is built:

1. **Headers die after 5 minutes** and the nonce is single-use — regenerate for
   every attempt.
2. **Query parameters are signed.** Changing a parameter in the Swagger form
   after generating headers invalidates them; pass the same `-p key=value` to the
   generator and regenerate.
3. **The `/navapigateway` prefix is part of the signature** but not of the path
   shown in Swagger. The generator adds it.

Host is confirmed as `api.navfundservices.com`, and `GetFundList` sits under
`/api/v1/ClientMasterData/`. The controller segment for the portfolio endpoints
isn't in the written docs — read it off the Swagger page.

## Wiring (the only files outside this folder)

- `navdash/settings.py` — add `"navfund"` to `INSTALLED_APPS`
- `tracker/management/commands/export_nav_report.py` — `--reconcile`, `--date`
- `tracker/services/gsheets.py` — the extra columns, date-targeted sheet
  selection (it currently derives the target day from `generated_at`, which would
  write a back-dated reconcile into the wrong day's spreadsheet), conditional
  formatting
- `config/config.yaml` — `report_hour_et: 5`, diff thresholds
- `pytest.ini` — `testpaths` extended to collect `navfund/tests`

## Next step

Run `navfund_discover` against one known-good date per fund and design
`normalize.py` + `account_map.yaml` from the actual payloads. Blocked on
credentials: NAV documents request parameters for the portfolio endpoints but
**not their response fields**, so the schema has to be observed rather than
assumed.