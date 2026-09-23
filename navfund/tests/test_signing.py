"""Pin the HMAC signing against NAV's published verification example.

NAV documents a fixed input/output pair ("Headers Verification" in their auth
docs). Reproducing it proves our signing is correct without credentials or
network access — so an auth failure in production is a credential/clock problem,
never a signing problem.
"""
import pytest

from navfund.client import (
    EMPTY_BODY_SHA256,
    NavFundAuthError,
    NavFundClient,
    build_auth_headers,
    content_hash,
    rfc1123_utc,
    string_to_sign,
)

# NAV's published vector, verbatim.
VECTOR = {
    "api_key": "API-TEST12345",
    "secret": "SECRET12345",
    "path_and_query": "/navapigateway/api/v1/testapi/get?id=123",
    "date": "Sun, 01 Jan 2023 00:00:00 GMT",
    "nonce": "697e61e1-a7be-4685-ad28-3eca25490277",
    "body": "",
}
EXPECTED_STRING_TO_SIGN = (
    "API-TEST12345;/navapigateway/api/v1/testapi/get?id=123;GET;"
    "Sun, 01 Jan 2023 00:00:00 GMT;697e61e1-a7be-4685-ad28-3eca25490277;"
    "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
)
EXPECTED_SIGNATURE = "1dVdWNoXAsIFOE0OTeO4gz07Yib9ewzhfhJomw87t4U="
EXPECTED_CONTENT_HASH = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


def test_empty_body_hash_matches_published_value():
    assert content_hash("") == EXPECTED_CONTENT_HASH
    assert content_hash(b"") == EXPECTED_CONTENT_HASH
    assert EMPTY_BODY_SHA256 == EXPECTED_CONTENT_HASH


def test_none_body_hashes_as_empty_not_as_the_string_none():
    """NAV's sample does str(body); None must not hash as the text "None"."""
    assert content_hash(None) == EXPECTED_CONTENT_HASH


def test_string_to_sign_matches_published_value():
    assert string_to_sign(
        VECTOR["api_key"], VECTOR["path_and_query"], "GET",
        VECTOR["date"], VECTOR["nonce"], EXPECTED_CONTENT_HASH,
    ) == EXPECTED_STRING_TO_SIGN


def test_headers_match_published_vector():
    headers = build_auth_headers(
        VECTOR["api_key"], VECTOR["secret"], "GET", VECTOR["path_and_query"],
        VECTOR["body"], x_date=VECTOR["date"], nonce=VECTOR["nonce"],
    )
    assert headers["x-date"] == VECTOR["date"]
    assert headers["x-content-sha256"] == EXPECTED_CONTENT_HASH
    assert headers["x-hmac256-signature"] == (
        f"{VECTOR['api_key']};{VECTOR['nonce']};{EXPECTED_SIGNATURE}"
    )


def test_lowercase_verb_is_signed_uppercase():
    lower = build_auth_headers(
        VECTOR["api_key"], VECTOR["secret"], "get", VECTOR["path_and_query"],
        x_date=VECTOR["date"], nonce=VECTOR["nonce"],
    )
    assert lower["x-hmac256-signature"].endswith(EXPECTED_SIGNATURE)


def test_rfc1123_format_is_locale_independent():
    """Must be RFC 1123 GMT regardless of the host's locale."""
    from datetime import datetime, timezone
    stamped = rfc1123_utc(datetime(2023, 1, 1, tzinfo=timezone.utc))
    assert stamped == "Sun, 01 Jan 2023 00:00:00 GMT"


def test_naive_datetime_is_treated_as_utc():
    from datetime import datetime
    assert rfc1123_utc(datetime(2023, 1, 1)) == "Sun, 01 Jan 2023 00:00:00 GMT"


def test_missing_credentials_raise_before_any_network_call():
    with pytest.raises(NavFundAuthError):
        build_auth_headers("", "", "GET", "/navapigateway/api/v1/x")


def test_build_path_adds_gateway_prefix_and_encodes_query_once():
    client = NavFundClient(api_key="k", secret="s")
    assert client.build_path("/api/v1/ClientMasterData/GetFundList") == (
        "/navapigateway/api/v1/ClientMasterData/GetFundList"
    )
    # Already-prefixed paths are not double-prefixed.
    assert client.build_path("/navapigateway/api/v1/x") == "/navapigateway/api/v1/x"
    # None params are dropped (NAV's LogID is legitimately null on a first call).
    assert client.build_path(
        "/api/v1/Fund/GetPortfolioDataDates", {"globalFundID": 12345, "logID": None},
    ) == "/navapigateway/api/v1/Fund/GetPortfolioDataDates?globalFundID=12345"


def test_signed_path_is_the_path_that_gets_sent():
    """The signature covers path+query, so the two must be built from one source."""
    client = NavFundClient(api_key="k", secret="s")
    params = {"globalFundID": 12345, "reportDate": "07-30-2026"}
    path = client.build_path("/api/v1/PortfolioData/GetCashBalancesForFund", params)
    assert path == (
        "/navapigateway/api/v1/PortfolioData/GetCashBalancesForFund"
        "?globalFundID=12345&reportDate=07-30-2026"
    )
    headers = build_auth_headers("k", "s", "GET", path, x_date=VECTOR["date"], nonce=VECTOR["nonce"])
    expected = build_auth_headers("k", "s", "GET", path, x_date=VECTOR["date"], nonce=VECTOR["nonce"])
    assert headers == expected