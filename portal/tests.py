"""Tests for the startup checks that keep an unsafe deploy from serving."""

from django.test import TestCase, override_settings

from catalog.models import Dashboard
from portal.checks import (
    DEV_SECRET,
    dashboards_are_not_publicly_addressable,
    deployment_is_locked_down,
)


def ids(issues):
    return {i.id for i in issues}


class DeploymentCheckTests(TestCase):
    @override_settings(TESTING=False, DEBUG=True, SECRET_KEY=DEV_SECRET, ALLOWED_HOSTS=["*"])
    def test_development_is_left_alone(self):
        self.assertEqual(deployment_is_locked_down(None), [])

    @override_settings(TESTING=False, DEBUG=False, SECRET_KEY=DEV_SECRET, ALLOWED_HOSTS=["dash.example.com"],
                       SESSION_COOKIE_SECURE=True, CSRF_TRUSTED_ORIGINS=["https://x"])
    def test_dev_secret_in_production_is_an_error(self):
        self.assertIn("portal.E001", ids(deployment_is_locked_down(None)))

    @override_settings(TESTING=False, DEBUG=False, SECRET_KEY="a-real-key", ALLOWED_HOSTS=[],
                       SESSION_COOKIE_SECURE=True, CSRF_TRUSTED_ORIGINS=["https://x"])
    def test_empty_allowed_hosts_is_an_error(self):
        self.assertIn("portal.E002", ids(deployment_is_locked_down(None)))

    @override_settings(TESTING=False, DEBUG=False, SECRET_KEY="a-real-key", ALLOWED_HOSTS=["*"],
                       SESSION_COOKIE_SECURE=True, CSRF_TRUSTED_ORIGINS=["https://x"])
    def test_wildcard_allowed_hosts_is_an_error(self):
        self.assertIn("portal.E003", ids(deployment_is_locked_down(None)))

    @override_settings(TESTING=False, DEBUG=False, SECRET_KEY="a-real-key", ALLOWED_HOSTS=["dash.example.com"],
                       SESSION_COOKIE_SECURE=False, CSRF_TRUSTED_ORIGINS=["https://x"])
    def test_insecure_cookies_warn(self):
        self.assertIn("portal.W001", ids(deployment_is_locked_down(None)))

    @override_settings(TESTING=False, DEBUG=False, SECRET_KEY="a-real-key", ALLOWED_HOSTS=["dash.example.com"],
                       SESSION_COOKIE_SECURE=True, CSRF_TRUSTED_ORIGINS=["https://x"])
    def test_a_correct_production_config_is_silent(self):
        self.assertEqual(deployment_is_locked_down(None), [])


class UpstreamAddressCheckTests(TestCase):
    def test_loopback_upstreams_are_fine(self):
        Dashboard.objects.create(slug="a", name="A", kind=Dashboard.PROXIED,
                                 upstream="http://127.0.0.1:8000")
        Dashboard.objects.create(slug="b", name="B", kind=Dashboard.PROXIED,
                                 upstream="http://localhost:4310")
        self.assertEqual(dashboards_are_not_publicly_addressable(None), [])

    def test_public_upstream_warns(self):
        Dashboard.objects.create(slug="c", name="C", kind=Dashboard.PROXIED,
                                 upstream="https://nav.example.com")
        self.assertIn("portal.W003", ids(dashboards_are_not_publicly_addressable(None)))

    def test_disabled_dashboard_is_ignored(self):
        Dashboard.objects.create(slug="d", name="D", kind=Dashboard.PROXIED,
                                 upstream="https://nav.example.com", enabled=False)
        self.assertEqual(dashboards_are_not_publicly_addressable(None), [])