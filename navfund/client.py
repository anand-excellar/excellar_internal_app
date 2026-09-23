"""HMAC-SHA256 signed transport for the NAV Fund Services API gateway.

NAV authenticates every request with three headers — ``x-date``,
``x-content-sha256`` and ``x-hmac256-signature`` — over a string-to-sign of::

    [API_KEY];[path+query];[VERB];[x-date];[nonce];[base64 sha256 of body]

Correctness of this module is pinned by NAV's own published test vector
(``navfund/tests/test_signing.py``), so signing can be verified without
credentials or network access.

Four details in here each exist for a reason:

* **The signed path must include the ``/navapigateway`` prefix** and must be
  byte-identical to what goes on the wire. We therefore build the query string
  ourselves and pass a complete URL to ``requests`` — never ``params=``, which
  would re-encode and silently break the signature.
* **``x-date`` is built with ``email.utils``, not ``strftime``.** NAV's own
  Python sample uses ``strftime("%a, %d %b %Y ...")``, whose day/month names are
  locale-dependent — on a non-English host that produces an unparseable date and
  every request 401s. ``format_datetime(usegmt=True)`` is always RFC 1123 C locale.
* **The body hash is required even for GET** (empty body → the constant below).
* **The nonce is replay protection**, so a retry must be re-signed with a fresh
  nonce and date rather than replaying the previous headers.

Credentials come from the environment (``.env``), never source control:
``NAVFUND_API_KEY``, ``NAVFUND_API_SECRET``, optionally ``NAVFUND_API_HOST``.
"""
import base64
import hashlib
import hmac
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# NAV publishes two hostnames across their docs (api.navfundservices.com in the
# auth section, api.navconsulting.com in the PHP sample). Ours is confirmed per
# tenant and overridable via env.
DEFAULT_HOST = "api.navfundservices.com"

# Every gateway path is prefixed with this, and the prefix is part of the
# string-to-sign — omitting it is the single most common signing failure.
GATEWAY_PREFIX = "/navapigateway"

# base64(sha256(b"")) — the value NAV's verification example expects for a
# request with no body. Kept as a constant so the empty-body path is obvious.
EMPTY_BODY_SHA256 = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="

# NAV rejects any request whose x-date is more than this far from their UTC
# clock, so a wrong host clock shows up as a 401 rather than a timeout.
CLOCK_SKEW_TOLERANCE_MINUTES = 5

DEFAULT_TIMEOUT = 120


def load_credentials_env() -> None:
    """Load the project's ``.env`` exactly as ``portal/settings.py`` does.

    Lets non-Django entry points (the CLI, ad-hoc scripts) pick up the same
    credentials the scheduled Django run uses, so the two can never disagree
    about which key is in force.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # A .env inside this package would work for a local script but be invisible
    # to Django, so the CLI would succeed while the scheduled job kept failing.
    stray = os.path.join(root, "navfund", ".env")
    if os.path.exists(stray):
        print(f"WARNING: {stray} is not read by anything — Django loads the "
              f"project's .env. Move NAVFUND_API_KEY/SECRET there.\n", file=sys.stderr)

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = os.path.join(root, ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file, override=True)


class NavFundAuthError(RuntimeError):
    """Missing/rejected credentials, or a signature NAV would not accept."""


class NavFundApiError(RuntimeError):
    """The gateway answered, but not with success."""


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def content_hash(body: bytes | str | None) -> str:
    """base64(sha256(body)) — the ``x-content-sha256`` header value.

    ``None`` and ``""`` both hash as an empty body. (NAV's sample does
    ``str(body)``, which turns ``None`` into the literal ``"None"`` and produces
    a hash the gateway rejects.)
    """
    if body is None:
        body = b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def rfc1123_utc(moment: datetime | None = None) -> str:
    """Now (or ``moment``) as e.g. ``Sun, 01 Jan 2023 00:00:00 GMT``.

    Locale-independent by construction — see the module docstring.
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return format_datetime(moment.astimezone(timezone.utc), usegmt=True)


def string_to_sign(
    api_key: str,
    path_and_query: str,
    method: str,
    x_date: str,
    nonce: str,
    body_hash: str,
) -> str:
    """The exact semicolon-joined payload NAV signs. No whitespace anywhere."""
    return ";".join([api_key, path_and_query, method.upper(), x_date, nonce, body_hash])


def build_auth_headers(
    api_key: str,
    secret: str,
    method: str,
    path_and_query: str,
    body: bytes | str | None = b"",
    *,
    x_date: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the three auth headers for one request.

    ``x_date`` and ``nonce`` are injectable so the published test vector can be
    reproduced; leave them unset in production so each request is freshly
    stamped and gets its own replay-protection nonce.
    """
    if not api_key or not secret:
        raise NavFundAuthError(
            "NAV Fund Services credentials missing — set NAVFUND_API_KEY and "
            "NAVFUND_API_SECRET in .env"
        )

    x_date = x_date or rfc1123_utc()
    nonce = nonce or str(uuid.uuid4())
    body_hash = content_hash(body)

    payload = string_to_sign(api_key, path_and_query, method, x_date, nonce, body_hash)
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("ascii")

    return {
        "x-date": x_date,
        "x-content-sha256": body_hash,
        "x-hmac256-signature": f"{api_key};{nonce};{signature}",
    }


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class NavFundClient:
    """Thin signed-GET client. Endpoint wrappers live in ``endpoints.py``."""

    def __init__(
        self,
        api_key: str | None = None,
        secret: str | None = None,
        host: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or os.environ.get("NAVFUND_API_KEY", "")
        self.secret = secret or os.environ.get("NAVFUND_API_SECRET", "")
        self.host = host or os.environ.get("NAVFUND_API_HOST", DEFAULT_HOST)
        self.timeout = timeout
        self._session = requests.Session()

    def build_path(self, endpoint: str, params: dict | None = None) -> str:
        """``/navapigateway/api/v1/...?a=1`` — what we sign *and* what we send.

        Query values are encoded once, here, so the signed string and the wire
        request cannot drift apart.
        """
        path = endpoint if endpoint.startswith(GATEWAY_PREFIX) else GATEWAY_PREFIX + endpoint
        cleaned = {k: v for k, v in (params or {}).items() if v is not None}
        return f"{path}?{urlencode(cleaned)}" if cleaned else path

    def get(self, endpoint: str, params: dict | None = None, *, retries: int = 2):
        """Signed GET returning parsed JSON (or raw text if not JSON).

        Each attempt is re-signed: the nonce is single-use replay protection, so
        reusing headers on a retry would be rejected as a replayed request.
        """
        path_and_query = self.build_path(endpoint, params)
        url = f"https://{self.host}{path_and_query}"

        last_error = None
        for attempt in range(1, retries + 1):
            headers = build_auth_headers(
                self.api_key, self.secret, "GET", path_and_query, b"",
            )
            try:
                resp = self._session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("NAV API %s attempt %d transport error: %s",
                               endpoint, attempt, exc)
                continue

            if resp.status_code == 401:
                # The signature itself did not validate.
                raise NavFundAuthError(
                    f"NAV API could not authenticate {endpoint} (401). Our signing is "
                    f"pinned to NAV's published test vector, so suspect (in order): a "
                    f"wrong API key or secret; a signed path missing the "
                    f"{GATEWAY_PREFIX} prefix; or this host's clock more than "
                    f"{CLOCK_SKEW_TOLERANCE_MINUTES} minutes off UTC. "
                    f"Body: {resp.text[:300]}"
                )
            if resp.status_code == 403:
                # Distinct from 401: the gateway declined the request rather than
                # failing to verify it — an unrecognised key, a key not entitled
                # to this endpoint, or a source IP that is not allowlisted.
                raise NavFundAuthError(
                    f"NAV API forbade {endpoint} (403). The signature was not the "
                    f"problem — the gateway declined the request. Check that the API "
                    f"key is exactly as issued (no added prefix), that your key is "
                    f"entitled to this endpoint, and that this host's IP is "
                    f"allowlisted. Body: {resp.text[:300]}"
                )
            if resp.status_code >= 500:
                last_error = NavFundApiError(f"{resp.status_code}: {resp.text[:300]}")
                logger.warning("NAV API %s attempt %d server error %s",
                               endpoint, attempt, resp.status_code)
                continue
            if not resp.ok:
                raise NavFundApiError(
                    f"NAV API {endpoint} failed {resp.status_code}: {resp.text[:500]}"
                )

            try:
                return resp.json()
            except ValueError:
                return resp.text

        raise NavFundApiError(f"NAV API {endpoint} failed after {retries} attempts: {last_error}")