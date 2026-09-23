import os
import sys
from pathlib import Path

import dj_database_url
import huey
import redis
import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# override=True keeps .env authoritative over any stale value already in the
# shell environment (e.g. from a prior `set -a; . .env`), which otherwise
# silently shadows edits to the file.
load_dotenv(BASE_DIR / ".env", override=True)

# NAV's collector settings (wallets, tokens, schedules, on-chain markets) come
# from config.yaml — deployment data that is not secret, unlike .env.
with open(BASE_DIR / "config" / "config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

DEBUG = os.environ.get("DEBUG", "true").lower() == "true"

# The test runner forces DEBUG=False, which would otherwise trip the production
# hardening checks on every `manage.py test` run. Those checks are about a
# deployed instance, not a throwaway test database.
TESTING = "test" in sys.argv

# A blank `SECRET_KEY=` line in .env is treated as unset, not as an empty key —
# otherwise copying .env.example verbatim would hard-fail even in development.
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
    SECRET_KEY = "dev-secret-key-do-not-use-in-production"

ALLOWED_HOSTS = (
    ["*"] if DEBUG
    else [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
)

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "huey.contrib.djhuey",

    # Portal: login, user management, the dashboard picker, and the gateway for
    # any dashboard that is not an app in this project.
    "accounts",
    "catalog",
    "gateway",

    # Dashboards and their shared machinery.
    "tracker",     # NAV data model, collectors and scheduled tasks
    "api",         # NAV REST API
    "nav",         # NAV dashboard UI
    "callspread",  # CallSpread tracker (ported from Node)
    "navfund",     # NAV Fund Services reconciliation
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "portal.wsgi.application"

# One database for the whole project: users, the dashboard catalog and NAV's
# snapshot history. None of the models use Postgres-only field types, so SQLite
# is the zero-setup default and `manage.py migrate` just works.
#
# In production set DATABASE_URL to Postgres. Pointing it at the existing navdash
# database keeps every NAV snapshot already recorded — the tracker app and its
# migrations came over unchanged, so the table names still match.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'data' / 'excellar.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

USE_TZ = True
TIME_ZONE = os.environ.get("PORTAL_TIME_ZONE", "UTC")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# --- cookies -----------------------------------------------------------------
# Proxied dashboards are themselves cookie-setting apps, and navdash is Django
# too — it would otherwise set its own "sessionid" at the root path and clobber
# the portal's login. Namespacing the portal's cookies makes that collision
# impossible; gateway.proxy additionally pins each upstream's cookies to its own
# /d/<slug>/ path so they never travel back to the portal.
SESSION_COOKIE_NAME = "portal_sessionid"
CSRF_COOKIE_NAME = "portal_csrftoken"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", 60 * 60 * 12))
SESSION_EXPIRE_AT_BROWSER_CLOSE = (
    os.environ.get("SESSION_EXPIRE_AT_BROWSER_CLOSE", "false").lower() == "true"
)

if not DEBUG:
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
    SECURE_CONTENT_TYPE_NOSNIFF = True

    # In production the portal sits behind nginx, which terminates TLS. Without
    # these, Django believes every request arrived over plain HTTP: it would
    # then refuse to set Secure cookies and reject the login as a CSRF failure.
    #
    # Only safe because nginx overwrites both headers on every request (see
    # deploy/nginx.conf) — a client cannot forge them. Set TRUST_PROXY_HEADERS
    # =false if you ever expose gunicorn directly.
    if os.environ.get("TRUST_PROXY_HEADERS", "true").lower() == "true":
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
        USE_X_FORWARDED_HOST = True

# --- CallSpread Tracker -------------------------------------------------------
# STS credentials come from the environment (STS_CLIENT_ID / STS_CLIENT_SECRET,
# optionally STS_API_URL / STS_AUTH_URL / STS_AUDIENCE) and are read by
# callspread.sts.client_from_env. With none set the dashboard serves its demo
# book rather than refusing to start.
CALLSPREAD = {
    "data_dir": os.environ.get("CALLSPREAD_DATA_DIR", str(BASE_DIR / "data" / "callspread")),
    "config_path": os.environ.get(
        "CALLSPREAD_CONFIG_PATH", str(BASE_DIR / "data" / "callspread" / "config.json")),
    "force_demo": os.environ.get("CALLSPREAD_DEMO", "false").lower() == "true",
    # Shares SLACK_WEBHOOK_URL with the NAV reconciliation alerts — one channel,
    # one env var. Alerts auto-enable once this is set (see callspread/alerts.py).
    "slack_webhook_url": os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
}

# --- gateway -----------------------------------------------------------------
# Still available for any dashboard that isn't a Django app in this project —
# a Dashboard row with kind="proxied" is served through it. NAV and CallSpread
# are internal apps and do not use it.
# Seconds to wait on an upstream dashboard. The connect timeout is short so a
# dead dashboard renders the "offline" page promptly instead of hanging the tab.
GATEWAY_CONNECT_TIMEOUT = float(os.environ.get("GATEWAY_CONNECT_TIMEOUT", 3))
GATEWAY_READ_TIMEOUT = float(os.environ.get("GATEWAY_READ_TIMEOUT", 60))

# Health probe used by the picker page to show live/offline badges.
GATEWAY_PROBE_TIMEOUT = float(os.environ.get("GATEWAY_PROBE_TIMEOUT", 2))
# =============================================================================
# NAV Tracker
# =============================================================================
# Carried over from the standalone navdash project's settings.py. The collector
# reads these; nothing here is portal configuration.

NAV_WALLETS = _cfg.get("wallets", {})
NAV_CROSS_MTM = _cfg.get("cross_mtm", {})
NAV_EXCHANGE = _cfg.get("exchange", {})
NAV_SNAPSHOT_INTERVAL_MINUTES = _cfg["schedule"]["snapshot_interval_minutes"]
NAV_DAILY_PNL_HOUR = _cfg["schedule"]["daily_pnl_hour"]
NAV_DAILY_PNL_MINUTE = _cfg["schedule"]["daily_pnl_minute"]

# Optional new tokens key (preferred). Fall back to wallets for compatibility.
NAV_TOKENS = _cfg.get("tokens", NAV_WALLETS)

# Aave v3 on-chain markets. RPC endpoints come from env (kept out of config.yaml
# so secrets/keys aren't committed). A market is only queried if its RPC is set.
NAV_AAVE_MARKETS = [
    {
        "chain": _m.get("chain", ""),
        "pool": _m.get("pool", ""),
        "rpc_url": os.environ.get(_m.get("rpc_url_env", ""), ""),
    }
    for _m in (_cfg.get("aave", {}) or {}).get("markets", [])
]


# Spark Savings (sUSDS) on-chain markets + rate source. Same env-driven RPC
# convention as Aave: a market is only queried if its RPC env var is set. The
# rate_source is where the global sUSDS->USDS rate is read (convertToAssets).
def _spark_market(_m):
    return {
        "chain": _m.get("chain", ""),
        "token": _m.get("token", ""),
        "rpc_url": os.environ.get(_m.get("rpc_url_env", ""), "").strip(),
    }


_spark_cfg = _cfg.get("spark", {}) or {}
NAV_SPARK = {
    "markets": [_spark_market(_m) for _m in _spark_cfg.get("markets", [])],
    "rate_source": (
        _spark_market(_spark_cfg["rate_source"])
        if _spark_cfg.get("rate_source") else None
    ),
}

# ERC-4626 vault positions (MetaMorpho and friends). Same env-driven RPC
# convention as Aave/Spark. asset_decimals is the underlying's, not the share
# token's — see tracker/scrapers/vaults.py.
NAV_VAULTS = [
    {
        "name": _v.get("name", ""),
        "symbol": _v.get("symbol", ""),
        "chain": _v.get("chain", ""),
        "vault": _v.get("vault", ""),
        "asset_symbol": _v.get("asset_symbol", ""),
        "asset_decimals": _v.get("asset_decimals"),
        "share_decimals": _v.get("share_decimals", 18),
        "rpc_url": os.environ.get(_v.get("rpc_url_env", ""), "").strip(),
    }
    for _v in (_cfg.get("vaults", {}) or {}).get("markets", [])
]

# API keys from env. Values are stripped so stray CRLF / whitespace from a
# Windows-edited .env can't leak into HTTP headers (requests rejects headers
# containing "\r"/"\n" as a header-injection risk).
#
# Each xltoken has its OWN exchange accounts, keyed by the uppercased segment
# name (e.g. XLBTC_BINANCE_API_KEY, XLETH_HL_WALLET_ADDRESS). The legacy
# asset-prefixed keys (BTC_/ETH_/USD_/...) are kept for backwards compatibility.
_EXCHANGE_KEY_SUFFIXES = [
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "HL_API_KEY", "HL_PRIVATE_KEY", "HL_WALLET_ADDRESS",
    "DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET",
]
_LEGACY_KEY_PREFIXES = ["BTC", "ETH", "USD", "USDC", "SOL"]
_ENV_KEY_NAMES = [
    f"{prefix}_{suffix}"
    for prefix in (_LEGACY_KEY_PREFIXES + [seg.upper() for seg in (NAV_TOKENS or {})])
    for suffix in _EXCHANGE_KEY_SUFFIXES
]
NAV_ENV_KEYS = {key: os.environ.get(key, "").strip() for key in _ENV_KEY_NAMES}

# Deribit — options/futures exchange equity, fetched per xltoken via the
# per-segment DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET keys above. Only the base
# URL is global; blank = production (www.deribit.com), set a test.deribit.com URL
# to target the testnet.
NAV_DERIBIT = {
    "base_url": os.environ.get("DERIBIT_BASE_URL", "").strip(),
}

# BitGo — non-exchange custody balances (replaces DeBank scraping).
# A single access token can reach every enterprise (XLtoken) it is authorized
# for. Each segment maps to its own enterprise via BITGO_ENTERPRISE_ID_<segment>
# (e.g. BITGO_ENTERPRISE_ID_xlBTC); the bare BITGO_ENTERPRISE_ID is the fallback
# for any segment without a specific override.
NAV_BITGO = {
    "access_token": os.environ.get("BITGO_ACCESS_TOKEN", "").strip(),
    "enterprise_id": os.environ.get("BITGO_ENTERPRISE_ID", "").strip(),
    "base_url": (os.environ.get("BITGO_BASE_URL", "") or "https://app.bitgo.com").strip(),
    "enterprise_by_segment": {
        seg: os.environ.get(f"BITGO_ENTERPRISE_ID_{seg}", "").strip()
        for seg in (NAV_TOKENS or {})
        if os.environ.get(f"BITGO_ENTERPRISE_ID_{seg}", "").strip()
    },
}

# NAV catalog report export -> Google Sheets/Drive. Reuses the same OAuth client
# (credentialsNAV.json) as the NAV_File_Tracking buildNAVSummary.py pipeline; by
# default it also reuses that pipeline's cached token (token_summary.pickle),
# which already has the Drive + Sheets scopes so scheduled/headless runs refresh
# without a prompt. hour_et is the local-Eastern hour the schedule fires.
#
# A copy of the Google credentials/token/sheet-id-cache lives here, inside the
# project, so NAV Tracker and the Covered Call Spread Risk Monitor are
# self-contained. The originals stay at NAV_File_Tracking untouched — other
# scripts (buildNAVSummary.py, moveNavFilestoDrive.py) still read those.
_NAV_FILES = Path(os.environ.get(
    "NAV_REPORT_FILES_DIR", str(BASE_DIR / "data" / "nav_report")))
NAV_REPORT = {
    "credentials_file": os.environ.get(
        "GOOGLE_CREDENTIALS_FILE", str(_NAV_FILES / "credentialsNAV.json")),
    "token_file": os.environ.get(
        "NAV_REPORT_TOKEN_FILE", str(_NAV_FILES / "token_summary.pickle")),
    "sheet_id_file": os.environ.get(
        "NAV_REPORT_SHEET_ID_FILE", str(_NAV_FILES / "nav_catalog_report_sheet_id.txt")),
    "drive_folder_name": os.environ.get("NAV_REPORT_DRIVE_FOLDER", "NAV_Daily_Reports"),
    "sheet_title": os.environ.get("NAV_REPORT_SHEET_TITLE", "NAV Catalog Report"),
    "hour_et": int(os.environ.get(
        "NAV_REPORT_HOUR_ET", _cfg.get("schedule", {}).get("report_hour_et", 5))),
    # Incoming webhook for the daily reconciliation post to Slack. Blank = the
    # report still gets written to Google Sheets, just not announced anywhere.
    "slack_webhook_url": os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
}

# Huey — the scheduler behind NAV's periodic snapshots and the daily report.
# RedisHuey in real use; tests swap in an immediate in-memory queue. Without a
# reachable Redis the web app still serves — only the schedule stops running.
#
# The pool is built here rather than handing Huey a bare url so protocol can be
# set. RESP2 is pinned because redis-py 8 opens a connection with HELLO, a
# command that only exists from Redis 6.0 — against the old Windows Redis build
# (3.0.504) every command then fails with "unknown command 'HELLO'". Every server
# version supports RESP2, current ones included, so this is portable rather than
# a local workaround that has to be undone on EC2.
_REDIS_URL = (os.environ.get("REDIS_URL", "") or "redis://localhost:6379/0").strip()
HUEY = huey.RedisHuey(
    "excellar",
    connection_pool=redis.ConnectionPool.from_url(
        _REDIS_URL,
        protocol=int(os.environ.get("REDIS_PROTOCOL", "2")),
    ),
)

# DRF — used by the NAV api app.
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
    # The whole project is behind the portal login; the API is no exception.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
