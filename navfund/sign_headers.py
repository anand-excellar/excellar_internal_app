"""Print ready-to-paste auth headers for a NAV API call.

For driving NAV's own Swagger "Execute" button (which does *not* sign for you —
it sends whatever you paste), for a quick curl, or as the first smoke test that a
new key/secret works at all.

    # Headers for a no-parameter call
    python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList

    # With query parameters (they are part of the signature)
    python -m navfund.sign_headers /api/v1/PortfolioData/GetCashBalancesForFund \
        -p globalFundID=12345 -p reportDate=07-30-2026

    # Ready-to-run curl, or just make the call and print the JSON
    python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList --curl
    python -m navfund.sign_headers /api/v1/ClientMasterData/GetFundList --execute

Credentials come from NAVFUND_API_KEY / NAVFUND_API_SECRET (or --api-key /
--secret). Headers expire 5 minutes after generation and the nonce is single-use,
so generate them immediately before pasting and regenerate for every attempt.
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):  # allow `python navfund/sign_headers.py ...`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from navfund.client import (  # noqa: E402
    CLOCK_SKEW_TOLERANCE_MINUTES,
    DEFAULT_HOST,
    NavFundClient,
    build_auth_headers,
    content_hash,
    rfc1123_utc,
    string_to_sign,
)


def _normalize_endpoint(raw: str) -> str:
    """Recover the real path from a shell-mangled argument.

    Git Bash / MSYS rewrites a leading-slash argument into a Windows path, so
    ``/api/v1/X`` arrives as ``C:/Program Files/Git/api/v1/X``. Everything before
    the ``/api/`` segment is therefore noise, and dropping it makes the command
    behave identically in PowerShell and Git Bash.
    """
    marker = "/api/"
    idx = raw.find(marker)
    if idx > 0:
        raw = raw[idx:]
    return raw if raw.startswith("/") else "/" + raw


def _load_dotenv_if_present() -> None:
    """Pick up credentials from the repo .env without needing Django loaded."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, ".env"), os.path.join(os.path.dirname(root), ".env")):
        if os.path.exists(candidate):
            load_dotenv(candidate, override=False)
            return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("endpoint",
                        help="e.g. /api/v1/ClientMasterData/GetFundList "
                             "(the /navapigateway prefix is added for you)")
    parser.add_argument("-p", "--param", action="append", default=[], metavar="KEY=VALUE",
                        help="query parameter; repeatable. Signed, so it must match "
                             "exactly what the request sends.")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--secret", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--curl", action="store_true", help="print a ready-to-run curl")
    parser.add_argument("--execute", action="store_true",
                        help="make the call now and print the response")
    args = parser.parse_args(argv)

    _load_dotenv_if_present()

    api_key = args.api_key or os.environ.get("NAVFUND_API_KEY", "")
    secret = args.secret or os.environ.get("NAVFUND_API_SECRET", "")
    host = args.host or os.environ.get("NAVFUND_API_HOST", DEFAULT_HOST)
    if not api_key or not secret:
        parser.error("No credentials. Set NAVFUND_API_KEY / NAVFUND_API_SECRET "
                     "in .env, or pass --api-key/--secret.")

    params = {}
    for item in args.param:
        if "=" not in item:
            parser.error(f"--param must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        params[key] = value

    client = NavFundClient(api_key=api_key, secret=secret, host=host)
    endpoint = _normalize_endpoint(args.endpoint)
    path_and_query = client.build_path(endpoint, params)
    url = f"https://{host}{path_and_query}"

    if args.execute:
        print(f"GET {url}\n")
        result = client.get(endpoint, params)
        print(json.dumps(result, indent=2) if not isinstance(result, str) else result)
        return 0

    x_date = rfc1123_utc()
    import uuid
    nonce = str(uuid.uuid4())
    body_hash = content_hash(b"")
    headers = build_auth_headers(api_key, secret, "GET", path_and_query,
                                 x_date=x_date, nonce=nonce)

    print(f"URL\n  {url}\n")
    print(f"String-to-sign\n  {string_to_sign(api_key, path_and_query, 'GET', x_date, nonce, body_hash)}\n")
    print("Paste these three into the portal (or send as headers):")
    for name in ("x-date", "x-content-sha256", "x-hmac256-signature"):
        print(f"  {name}: {headers[name]}")
    print(f"\nValid for {CLOCK_SKEW_TOLERANCE_MINUTES} minutes, single use — "
          f"regenerate for each attempt, and after changing any parameter.")

    if args.curl:
        header_args = " ".join(f"\\\n  -H '{k}: {v}'" for k, v in headers.items())
        print(f"\ncurl -X GET '{url}' \\\n  -H 'accept: application/json' {header_args}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())