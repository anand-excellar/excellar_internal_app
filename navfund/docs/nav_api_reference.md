# NAV Fund Services API — reference

Condensed transcription of NAV's published API docs
(<https://portal.navfundservices.com/navportalcore/help-documents/1>), captured
2026-07-31. The vendor portal is authoritative; this file exists so the details
we depend on are versioned next to the code.

Sample code for C#, Java, Go, PHP, Bash and PowerShell has been dropped — only
the Python sample is reproduced below, and `navfund/client.py` supersedes it (see
the deviations noted at the end).

---

## Authentication

HMAC-SHA256. Every request carries three headers:

| Header | Description |
|---|---|
| `x-date` | Origination time. **Cannot be more than 5 minutes off current UTC.** |
| `x-content-sha256` | base64 SHA256 hash of the request body. **Required even with no body.** |
| `x-hmac256-signature` | `[API_KEY];[nonce];[signature]` |

### String-to-sign

```
[API_KEY];[url];[httpVerb];[timestamp];[nonce];[base64 SHA256 hash of request body]
```

- `API_KEY` — unique client authentication key.
- `url` — request path **and query**, "in lower case in escape uri string" per the
  prose; every vendor sample in fact signs the path verbatim as sent, and the
  verification example below is already lowercase. **Must include the
  `/navapigateway` prefix** (the docs call this out twice).
- `httpVerb` — `GET` or `POST`.
- `timestamp` — same value as `x-date`, format `Mon, 01 Jan 2022 09:55:10 GMT`.
- `nonce` — a GUID, used to identify replay requests.

Semicolon-separated, no blank/white space.

### Headers Verification (published test vector)

Input:

```
APIKey       – "API-TEST12345"
Secret       – "SECRET12345"
API          – https://api.navfundservices.com/navapigateway/api/v1/testapi/get?id=123
Path+Query   – "/navapigateway/api/v1/testapi/get?id=123"
date         – "Sun, 01 Jan 2023 00:00:00 GMT"
nonce        – "697e61e1-a7be-4685-ad28-3eca25490277"
request body – ""
```

Output:

```
StringtoSign
API-TEST12345;/navapigateway/api/v1/testapi/get?id=123;GET;Sun, 01 Jan 2023 00:00:00 GMT;697e61e1-a7be-4685-ad28-3eca25490277;47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=

x-date              - Sun, 01 Jan 2023 00:00:00 GMT
x-hmac256-signature - API-TEST12345;697e61e1-a7be-4685-ad28-3eca25490277;1dVdWNoXAsIFOE0OTeO4gz07Yib9ewzhfhJomw87t4U=
x-content-sha256    - 47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=
```

This vector is pinned by `navfund/tests/test_signing.py` and reproduces exactly.

### Vendor Python sample (superseded by `client.py`)

```python
import base64, hashlib, hmac, uuid
from datetime import datetime

def sign_request(method, pathAndQuery, body, apiKey, secret):
    verb = method.upper()
    utc_now = str(datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S ")) + "GMT"
    nonce = str(uuid.uuid4())
    content_digest = hashlib.sha256(str(body).encode("utf-8")).digest()
    content_hash = base64.b64encode(content_digest).decode("utf-8")
    string_to_sign = apiKey +";"+ pathAndQuery +";"+ verb +";"+ utc_now +";"+ nonce +";"+ content_hash
    digest = hmac.new(secret.encode(), str(string_to_sign).encode("utf-8"), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    return {
        "x-date": utc_now,
        "x-hmac256-signature": apiKey +";"+ nonce +";"+ signature,
        "x-content-sha256": content_hash,
    }
```

**Where our client deliberately differs:**

1. `strftime("%a, %d %b ...")` is locale-dependent — on a non-English host it
   emits day/month names NAV cannot parse and every call 401s. We use
   `email.utils.format_datetime(..., usegmt=True)`.
2. `str(body)` turns `None` into the literal `"None"` and hashes that. We treat
   `None` and `""` alike as an empty body.
3. `datetime.utcnow()` is deprecated in 3.12; we use timezone-aware UTC.
4. We build path+query once and send that exact string, so the signature can
   never drift from the URL (never `requests`' `params=`).

### Host

**`api.navfundservices.com`** — confirmed from the curl the portal's Swagger page
emits for our tenant:
`https://api.navfundservices.com/navapigateway/api/v1/ClientMasterData/GetFundList`.
(`api.navconsulting.com` appears only in their PHP sample; ignore it.)
Overridable via `NAVFUND_API_HOST`.

### Controller path segments

Confirmed from the portal: `GetFundList` lives under
`/api/v1/ClientMasterData/`. The controller segment for the other groups
(portfolio, fund accounting) is **not** stated in the written docs — read each
one off the Swagger page rather than assuming, since the group headings
("Fund Master Data", "Portfolio Data") do not necessarily match the URL segment.

---

## Master endpoints

### Fund

**`GetFundList`** — list of funds. This is the entry point: it yields the
`GlobalFundID` every other call needs, plus the data-availability gates.

| Output | Meaning |
|---|---|
| `FundName` | Fund's reporting name (e.g. `XLBTC SA`) |
| `GlobalFundID` | Fund identifier in NAV's database |
| `FundEndDate` | Fund close date |
| `FundDailyAccountingStartDate` | First date daily *estimated* accounting exists |
| `FundDailyAccountingLastAvailableDate` | Most recent daily estimated accounting |
| `FundOfficialAccountingLastAvailableDate` | Most recent *official* accounting |
| `PortfolioLastAvailableDate` | Most recent daily estimated portfolio data. **Null ⇒ portfolio not set up for that fund.** |

**`GetEntityRelationshipOfFund`** (`GlobalFundID`) — the fund's underlying
entities (trader / trading class) → `EntityID`, `EntityName`, `RelationShip`,
relationship start/end dates, and per-entity accounting availability dates.

**`GetAccountingDataDates`** (`GlobalFundID`, `LogID`) — which accounting dates
have data. `LogID` is a differential cursor: pass `null` on the first call, then
the returned `LogID` on later calls to get only dates updated since. Returns
`LogID`, `FromDate`, `ToDate` (MM-DD-YYYY).

**`GetPortfolioDataDates`** (`GlobalFundID`, `LogID`) — same, for portfolio data.
Returns `LogID`, `FromDate`, `ToDate`.

### Investor

`GetFundListByEmail` (`Email`), `GetInvestorListbyEmail` (`Email`),
`GetInvestorListbyFund` (`GlobalFundID`) — investor/fund association and
per-investor availability dates. Not used by the reconciliation.

---

## Fund endpoints

### Shared date rules

Every date parameter is **MM-DD-YYYY**, and NAV repeats the same three
constraints throughout:

1. The requested date must be **≤ the relevant last-available date** from
   `GetFundList`: `FundDailyAccountingLastAvailableDate` for daily estimates,
   `FundOfficialAccountingLastAvailableDate` for official numbers,
   `PortfolioLastAvailableDate` for portfolio data.
2. The requested date must be **≥ the `FromDate`** returned by the first
   (`LogID = null`) call to `GetAccountingDataDates` / `GetPortfolioDataDates`.
3. Ranges must sit inside the `FromDate`/`ToDate` window from that first call.

### Accounting data

| Endpoint | Parameters |
|---|---|
| `GetBalanceSheetForFund` | `GlobalFundID`, `FromDate`, `ToDate` |
| `GetIncomeExpensesForFund` | `GlobalFundID`, `FromDate`, `ToDate` |
| `GetCapitalBalancesAndFeesForFund` | `GlobalFundID`, `FromDate`, `ToDate` |
| `GetPerformanceReturnsForFund` | `GlobalFundID`, `FromDate`, `ToDate` |
| `GetDetailedTrialBalanceForFund` | `GlobalFundID`, `Year` (YYYY), `Month` (1–12) — YTD trial balance with security-level break-up |
| `GetDailyDetailedTrialBalanceForFund` | `GlobalFundID`, `ReportDate` — trial balance for one date with security-level break-up |
| `GetEstimateNAVAndPerformanceReturnsForFund` | `GlobalFundID`, `FromDate`, `ToDate` — **daily estimated NAV** (gated on `FundDailyAccountingLastAvailableDate`) |
| `GetOfficialNAVAndPerformanceReturnsForFund` | `GlobalFundID`, `FromDate`, `ToDate` — official NAV (gated on `FundOfficialAccountingLastAvailableDate`) |

### Portfolio data

| Endpoint | Parameters |
|---|---|
| `GetCashBalancesForFund` | `GlobalFundID`, `ReportDate` |
| `GetTradingGainLossForFund` | `GlobalFundID`, `ReportDate` |
| `GetTradesForFund` | `GlobalFundID`, `ReportDate` |
| `GetRealizedTaxLotForFund` | `GlobalFundID`, `ReportDate` |
| `GetUnRealizedTaxLotForFund` | `GlobalFundID`, `ReportDate` |
| `GetCashTransactionForFundOnDataRange` | `GlobalFundID`, `FromDate`, `ToDate` |

All portfolio endpoints are gated on `PortfolioLastAvailableDate`.

### Investor demographic

`GetInvestorDemographicDataForFund` (`GlobalFundID`, `IsActive`). Not used here.

---

## Investor endpoints / E-Subscription

Investor balances, statements (zip streams), capital transactions, VAMI, and
e-subscription template/invitation management. **None are used by the
reconciliation** — recorded here only so the omission is deliberate.

---

## Gap in the docs

Request parameters are documented for every endpoint; **response schemas are
documented only for the Master endpoints.** The Fund → Portfolio Data endpoints
we depend on (`GetCashBalancesForFund` above all) document no output fields, so
the field names, the account/ticker granularity, and whether a stable account id
is exposed all have to be learned from a live response. That is what
`navfund_discover` exists to do — see `../README.md`.