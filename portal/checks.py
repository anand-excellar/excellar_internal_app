"""Startup checks that refuse to serve the portal in an unsafe configuration.

The portal is the only thing standing between the internet and dashboards that
have no authentication of their own. A silent misconfiguration here — a
leftover dev SECRET_KEY, ALLOWED_HOSTS=*, cookies sent in the clear — does not
degrade the product, it removes the lock. These run on every management command,
so a bad deploy fails at boot rather than at the first unauthorised page view.

Bypass an individual check with SKIP_DEPLOY_CHECKS=portal.E001,portal.W002 only
when you know why it is safe.
"""

import os
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Warning, register

DEV_SECRET = "dev-secret-key-do-not-use-in-production"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _skipped() -> set[str]:
    return {c.strip() for c in os.environ.get("SKIP_DEPLOY_CHECKS", "").split(",") if c.strip()}


@register()
def deployment_is_locked_down(app_configs, **kwargs):
    if settings.DEBUG or getattr(settings, "TESTING", False):
        # Development: the dashboards are on loopback anyway and nothing is
        # published. Checking here would only train people to ignore the output.
        # (The test runner forces DEBUG=False, hence the TESTING guard — the
        # tests below call this function directly with explicit settings.)
        return []

    issues = []

    if settings.SECRET_KEY == DEV_SECRET:
        issues.append(Error(
            "SECRET_KEY is still the development default.",
            hint="Anyone with this repo can forge a session cookie and walk past the "
                 "login. Generate one: python -c \"from django.core.management.utils "
                 "import get_random_secret_key as k; print(k())\"",
            id="portal.E001",
        ))

    if not settings.ALLOWED_HOSTS:
        issues.append(Error(
            "ALLOWED_HOSTS is empty while DEBUG is off.",
            hint="Set it to the hostname users will reach, e.g. dash.excellar.finance",
            id="portal.E002",
        ))
    elif "*" in settings.ALLOWED_HOSTS:
        issues.append(Error(
            "ALLOWED_HOSTS contains '*'.",
            hint="A wildcard allows Host-header attacks that can poison password-reset "
                 "and redirect URLs. Name the hostnames explicitly.",
            id="portal.E003",
        ))

    if not getattr(settings, "SESSION_COOKIE_SECURE", False):
        issues.append(Warning(
            "Session cookies may be sent over plain HTTP.",
            hint="Serve the portal behind TLS and leave COOKIE_SECURE at its default. "
                 "Without it, a credential on the wire is a credential in the open.",
            id="portal.W001",
        ))

    if not settings.CSRF_TRUSTED_ORIGINS:
        issues.append(Warning(
            "CSRF_TRUSTED_ORIGINS is empty.",
            hint="Behind a TLS-terminating proxy, logins can fail CSRF validation "
                 "without this. Set it to https://<your hostname>.",
            id="portal.W002",
        ))

    return [i for i in issues if i.id not in _skipped()]


@register()
def dashboards_are_not_publicly_addressable(app_configs, **kwargs):
    """An upstream on a public address defeats the portal entirely.

    The point of the gateway is that the only route to a dashboard runs through
    an authenticated session. If a Dashboard row points at a public hostname,
    users can simply open that instead.
    """
    try:
        from catalog.models import Dashboard
        dashboards = list(Dashboard.objects.filter(enabled=True))
    except Exception:
        # The database may not exist yet (first migrate) — nothing to check.
        return []

    issues = []
    for dash in dashboards:
        # Internal dashboards are views in this project — there is no separate
        # port in front of them, so there is nothing to bypass.
        if dash.is_internal:
            continue
        host = urlsplit(dash.upstream).hostname or ""
        if host in LOOPBACK_HOSTS or host.startswith(("10.", "192.168.", "172.")):
            continue
        issues.append(Warning(
            f"Dashboard '{dash.slug}' points at a non-private upstream ({host}).",
            hint="If that address is reachable by your users, they can open the "
                 "dashboard directly and skip the login. Run "
                 "`manage.py check_exposure` to confirm what is actually reachable.",
            id="portal.W003",
        ))
    return [i for i in issues if i.id not in _skipped()]