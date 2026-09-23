# Excellar Dashboards

One Django project, one language, one port. Users sign in once and pick a
dashboard; there is no way to reach a dashboard without an account.

```
                    browser
                       │
                       ▼
        ┌──────────────────────────────┐
        │  Excellar Dashboards  :8080  │
        │  ────────────────────────    │
        │  /            picker         │
        │  /login/      sign in        │
        │  /users/      user admin     │
        │  /d/nav/         NAV Tracker │
        │  /d/callspread/  CallSpread  │
        └──────────────────────────────┘
             one process · one command
```

| Path | What it is |
|------|-----------|
| `/` | Dashboard picker (every signed-in user sees every dashboard) |
| `/login/`, `/password/` | Sign in, change your own password |
| `/users/` | User management — staff only |
| `/admin/` | Django admin — staff only |
| `/d/nav/` | NAV Tracker |
| `/d/callspread/` | CallSpread Tracker |
| `/api/nav/…` | NAV REST API (session required) |

## Quick start

```powershell
cd C:\Users\aashi\Strategies\Excellar_Dashboards
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py seed_dashboards
.venv\Scripts\python.exe manage.py createsuperuser
.venv\Scripts\python.exe manage.py runserver 8080
```

Open <http://localhost:8080>. That is the whole application — no second server,
no Node, no proxy.

Two optional background processes add live data:

```powershell
# CallSpread: refresh the STS book every 60s
.venv\Scripts\python.exe manage.py callspread_poll

# NAV: periodic snapshots, daily P&L, the 5am ET report (needs Redis)
.venv\Scripts\python.exe manage.py run_huey
```

Neither is needed to browse: the web app serves the last snapshot on disk, and
`/d/callspread/` polls once inline if it finds none.

## Layout

```
portal/       settings, urls, production startup checks
accounts/     login, logout, user management screens
catalog/      the Dashboard registry behind the picker
gateway/      authenticated reverse proxy — see "Adding a dashboard"
nav/          NAV dashboard UI (was navdash's `dashboard` app)
tracker/      NAV data model, collectors, scheduled tasks
api/          NAV REST API
callspread/   CallSpread tracker, ported from Node to Python
config/       config.yaml — NAV wallets, tokens, schedules
templates/    portal pages
static/       portal CSS + Excellar brand assets
data/         SQLite DB, CallSpread history and config  (not in git)
tests/        NAV test suite
deploy/       nginx + systemd units for EC2
```

## Database

One database for everything — users, the dashboard catalog and NAV's snapshot
history. SQLite by default, so `migrate` works with nothing installed:

```
data/excellar.sqlite3
```

For production set `DATABASE_URL`:

```ini
DATABASE_URL=postgres://user:pass@localhost:5432/navdash
```

**Pointing this at the existing navdash Postgres keeps every NAV snapshot you
have already recorded.** The `tracker` app and its migrations came across
unchanged, so the table names still match; `migrate` adds the portal's own
tables (users, catalog) alongside them.

## The CallSpread port

CallSpread was a Node app. Its server is now Python; its browser code is the
original, byte for byte.

| Original | Now |
|----------|-----|
| `src/model.js` (greeks, coverage, P&L, roll) | `callspread/model.py` |
| `src/sts.js` | `callspread/sts.py` |
| `src/store.js` | `callspread/store.py` |
| `src/alerts.js` | `callspread/alerts.py` |
| `src/demo.js` | `callspread/demo.py` |
| `src/server.js` | `callspread/views.py` + `runtime.py` + `callspread_poll` |
| `public/app.js`, `charts.js`, `styles.css` | `callspread/static/callspread/` — **unchanged** |
| `public/index.html` | `callspread/templates/callspread/index.html` — two asset paths + an API-prefix shim |

Nothing about the maths was redesigned. The translation was checked against the
Node implementation on a real STS payload and every field of the snapshot,
history point and alert rules matched to 1e-12 — that capture is committed as
`callspread/tests/node_expected.json` and asserted by `test_parity.py`, so a
future edit that moves a number fails the suite and names the field.

The demo book matches too, including the seeded PRNG: `mulberry32` is ported with
32-bit arithmetic so the synthetic history draws the same path Node drew.

### Porting notes worth knowing

- **Timestamps.** STS sends `transactTime` with no timezone. JavaScript reads a
  zoneless date-*time* as local time, Python defaults to UTC — which shifted
  every trade timestamp. `callspread/model.py:parse_date` follows the JS rule so
  the port is faithful. That rule also means the original renders `openedAt`
  differently depending on the host's timezone; a pre-existing quirk, harmless on
  a UTC host like EC2.
- **`config.json`** now lives at `data/callspread/config.json`, seeded from
  `callspread/default_config.json`, because the Settings panel writes to it. The
  `server` block is gone — there is no separate port any more.
- **The poller is its own process** so history is appended once, not once per
  gunicorn worker.

## Adding a dashboard

Two ways, both a `Dashboard` row (`/admin/catalog/dashboard/`):

**Internal** (`kind="internal"`) — a Django app in this project. Add the app,
include its urls at `/d/<slug>/` in `portal/urls.py`, decorate its views with
`@login_required`, and add the row. This is how NAV and CallSpread work.

**Proxied** (`kind="proxied"`) — a separate program on loopback, in any language.
Set `upstream` to `http://127.0.0.1:<port>` and the gateway forwards to it after
checking the session, injecting a `<base>` tag and a fetch/XHR shim so an app
that doesn't know it is mounted under a prefix still works. Nothing uses this
today; it is what stops a future non-Python dashboard from forcing a rewrite.

## Why there is no way around the login

- Every view is `@login_required`; the NAV API uses DRF `IsAuthenticated`.
- Both dashboards are *in this process*. There is no second port to bypass —
  which is what the old setup relied on loopback binding to achieve.
- The app refuses to **start** in an unsafe production configuration: a leftover
  dev `SECRET_KEY` or `ALLOWED_HOSTS=*` fails the boot checks with a
  `portal.E00x` error rather than quietly serving ([portal/checks.py](portal/checks.py)).

Verify rather than assume:

```powershell
.venv\Scripts\python.exe manage.py check_exposure
```

It opens a socket to each dashboard's port over the machine's own LAN address —
what an outsider would do. Internal dashboards report "no separate port to
bypass"; a proxied one that answers is reported as a bypass.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

132 tests: the NAV suite, the CallSpread parity fixture and dashboard behaviour,
and the portal's auth, gateway and production-hardening checks. No servers or
Redis needed — the task queue runs inline under `portal.settings_test`.

There is also a browser check that drives a real Chromium against a running
instance:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe tests\e2e_browser.py --username aashish --password <yours>
```

## Deploying

See **[deploy/DEPLOY.md](deploy/DEPLOY.md)**. Three services — the web app, the
CallSpread poller, the NAV scheduler — behind nginx with TLS, with only 443 open.

## Configuration

`.env` holds secrets and is never committed. `.env.example` lists every key.

| Group | Keys |
|-------|------|
| Core | `DEBUG`, `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` |
| CallSpread | `STS_CLIENT_ID`, `STS_CLIENT_SECRET`, `STS_API_URL`, `STS_AUTH_URL`, `STS_AUDIENCE`, `CALLSPREAD_DEMO` |
| NAV custody | `BITGO_ACCESS_TOKEN`, `BITGO_ENTERPRISE_ID`, `BITGO_ENTERPRISE_ID_<segment>` |
| NAV exchanges | `<SEGMENT>_BINANCE_API_KEY`, `<SEGMENT>_HL_WALLET_ADDRESS`, `<SEGMENT>_DERIBIT_CLIENT_ID`, … |
| NAV on-chain | the RPC env vars named in `config/config.yaml` |
| NAV report | `GOOGLE_CREDENTIALS_FILE`, `NAV_REPORT_TOKEN_FILE`, `NAV_REPORT_SHEET_ID_FILE` |
| Scheduler | `REDIS_URL` |

`config/config.yaml` holds NAV's non-secret deployment data — wallets, tokens,
snapshot schedule, Aave/Spark markets.

With no `STS_*` keys set, CallSpread serves its demo book instead of failing.