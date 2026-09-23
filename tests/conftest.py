"""Shared test setup. Settings come from portal.settings_test.

Every NAV view and API endpoint now sits behind the portal login, so the tests
below — which were written against a standalone service with no auth — get a
signed-in client by default. The tests that assert the gating itself build their
own anonymous client instead (see gateway/tests.py, callspread/tests/).
"""

import pytest


@pytest.fixture(autouse=True)
def signed_in(db, client):
    """Log the shared test client in for the duration of each test."""
    from django.contrib.auth.models import User

    user = User.objects.create_user("tester", password="pw-for-tests-1234")
    client.force_login(user)
    return user


@pytest.fixture
def xleth_disabled(settings):
    """Disable the xlETH segment for tests about excluding disabled segments.

    These tests were written when xlETH was disabled in config.yaml. It has since
    been enabled, which silently broke them — a test asserting "a disabled
    segment is hidden" must control that flag itself rather than inherit whatever
    the deployment happens to be configured with.
    """
    import copy

    tokens = copy.deepcopy(settings.NAV_TOKENS)
    if "xlETH" in tokens:
        tokens["xlETH"]["enabled"] = False
    settings.NAV_TOKENS = tokens
    return tokens
