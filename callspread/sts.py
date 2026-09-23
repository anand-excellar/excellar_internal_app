"""STS Digital REST client — port of the original `src/sts.js`.

Auth is OAuth2 client_credentials against Auth0; the access token lives 60
minutes so we cache it and refresh a minute early.
Docs: https://sts-digital.github.io/core.api-docs/
"""

import os
import threading
import time

import requests

DEFAULTS = {
    "auth_url": "https://stsdigital.eu.auth0.com",
    "api_url": "https://tokyo.stsdigital.net",
    "audience": "https://api.stsdigital.net",
}

TIMEOUT = (5, 30)  # connect, read


class StsError(RuntimeError):
    pass


class StsClient:
    def __init__(self, client_id=None, client_secret=None, auth_url=None,
                 api_url=None, audience=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_url = (auth_url or DEFAULTS["auth_url"]).rstrip("/")
        self.api_url = (api_url or DEFAULTS["api_url"]).rstrip("/")
        self.audience = audience or DEFAULTS["audience"]
        self._token = None
        self._token_expiry = 0.0
        # Collapses concurrent refreshes onto one request, as the Node version's
        # in-flight promise did.
        self._token_lock = threading.Lock()
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        with self._token_lock:
            if self._token and time.time() < self._token_expiry:
                return self._token
            resp = self._session.post(
                f"{self.auth_url}/oauth/token",
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "audience": self.audience,
                    "grant_type": "client_credentials",
                },
                timeout=TIMEOUT,
            )
            if not resp.ok:
                raise StsError(f"STS auth failed ({resp.status_code}): {resp.text[:400]}")
            body = resp.json()
            self._token = body["access_token"]
            # Refresh 60s before the stated expiry.
            self._token_expiry = time.time() + max(30, (body.get("expires_in") or 3600) - 60)
            return self._token

    def get(self, path: str, params: dict = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.api_url + path
        headers = {"authorization": f"Bearer {self.token()}", "accept": "application/json"}
        resp = self._session.get(url, params=params, headers=headers, timeout=TIMEOUT)

        if resp.status_code == 401:
            # Token rejected mid-flight: drop the cache and retry exactly once.
            self._token = None
            self._token_expiry = 0.0
            headers["authorization"] = f"Bearer {self.token()}"
            resp = self._session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if not resp.ok:
                raise StsError(f"STS {path} failed ({resp.status_code})")
            return resp.json()

        if not resp.ok:
            raise StsError(f"STS {path} failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def positions(self, **opts):
        return self.get("/v1alpha/positions", opts)

    def accounts(self):
        return self.get("/v1alpha/accounts")

    def instruments(self, type="option", venue="sts"):
        """`type` and `venue` are REQUIRED — omitting them 400s with "The type
        field is required." despite the docs example showing a bare call.
        type: 'option' | 'spot' (note: 'options' plural returns an empty array
        rather than an error, so a typo looks like "no instruments").

        Returns instrument DEFINITIONS only — strike, expiry, state, rfqActive.
        There are no prices on this endpoint.
        """
        return self.get("/v1alpha/instruments", {"type": type, "venue": venue})

    def trades(self, account=None, limit=200, before=None):
        return self.get("/v1alpha/trades", {"account": account, "limit": limit, "before": before})

    def orders(self, account=None, limit=200, only_open=None):
        return self.get("/v1alpha/orders", {"account": account, "limit": limit, "onlyOpen": only_open})


def client_from_env(env=None) -> StsClient:
    env = env if env is not None else os.environ
    return StsClient(
        client_id=env.get("STS_CLIENT_ID"),
        client_secret=env.get("STS_CLIENT_SECRET"),
        auth_url=env.get("STS_AUTH_URL"),
        api_url=env.get("STS_API_URL"),
        audience=env.get("STS_AUDIENCE"),
    )