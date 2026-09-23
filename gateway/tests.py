"""Tests for the parts of the gateway that fail silently when wrong.

URL rewriting and auth gating are the risky surface: a mistake there either
breaks a dashboard subtly (assets 404, htmx stops updating) or — worse — leaves
a dashboard reachable without a login. Both are cheap to pin down.
"""

from unittest import mock

import requests
from django.contrib.auth.models import User
from django.test import Client, TestCase

from catalog.models import Dashboard
from gateway.rewrite import inject_base_and_shim, rewrite_location, rewrite_set_cookie

MOUNT = "/d/nav"


class RewriteSetCookieTests(TestCase):
    def test_existing_path_is_nested_under_the_mount(self):
        out = rewrite_set_cookie("sessionid=abc; Path=/; HttpOnly", MOUNT)
        self.assertIn("Path=/d/nav/", out)
        self.assertIn("HttpOnly", out)
        self.assertIn("sessionid=abc", out)

    def test_non_root_path_is_preserved_beneath_the_mount(self):
        out = rewrite_set_cookie("a=1; Path=/admin/", MOUNT)
        self.assertIn("Path=/d/nav/admin/", out)

    def test_missing_path_gets_the_mount(self):
        self.assertEqual(rewrite_set_cookie("a=1; HttpOnly", MOUNT), "a=1; HttpOnly; Path=/d/nav/")

    def test_other_attributes_survive(self):
        out = rewrite_set_cookie("a=1; Path=/; Secure; SameSite=Lax", MOUNT)
        self.assertIn("Secure", out)
        self.assertIn("SameSite=Lax", out)


class RewriteLocationTests(TestCase):
    def test_root_relative_redirect_is_mounted(self):
        self.assertEqual(rewrite_location("/partials/x/", MOUNT, "http://127.0.0.1:8000"),
                         "/d/nav/partials/x/")

    def test_absolute_upstream_redirect_is_mounted(self):
        self.assertEqual(
            rewrite_location("http://127.0.0.1:8000/admin/", MOUNT, "http://127.0.0.1:8000"),
            "/d/nav/admin/")

    def test_already_mounted_redirect_is_left_alone(self):
        self.assertEqual(rewrite_location("/d/nav/x/", MOUNT, "http://127.0.0.1:8000"), "/d/nav/x/")

    def test_external_redirect_is_left_alone(self):
        target = "https://accounts.google.com/o/oauth2/auth"
        self.assertEqual(rewrite_location(target, MOUNT, "http://127.0.0.1:8000"), target)


class InjectionTests(TestCase):
    def test_base_and_shim_land_inside_head(self):
        body = inject_base_and_shim(b"<html><head><link href='a.css'></head></html>", MOUNT)
        self.assertIn(b'<base href="/d/nav/">', body)
        self.assertLess(body.index(b"<base"), body.index(b"<link"),
                        "the base tag must precede the page's own assets")

    def test_page_without_head_still_gets_the_shim(self):
        body = inject_base_and_shim(b"<html><body>hi</body></html>", MOUNT)
        self.assertIn(b'<base href="/d/nav/">', body)

    def test_mount_is_interpolated_into_the_shim(self):
        body = inject_base_and_shim(b"<html><head></head></html>", "/d/callspread")
        self.assertIn(b'var P = "/d/callspread"', body)


class AccessControlTests(TestCase):
    """The whole point of the portal: nothing is reachable without a login."""

    def setUp(self):
        # A proxied dashboard needs a slug that is NOT an internal app, or the
        # project's own /d/<slug>/ route would answer before the gateway does.
        Dashboard.objects.create(
            slug="legacy", name="Legacy Tracker", kind=Dashboard.PROXIED,
            upstream="http://127.0.0.1:8000")
        Dashboard.objects.create(
            slug="hidden", name="Disabled", kind=Dashboard.PROXIED,
            upstream="http://127.0.0.1:9999", enabled=False)
        self.user = User.objects.create_user("viewer", password="pw-for-tests-1234")
        self.staff = User.objects.create_user(
            "boss", password="pw-for-tests-1234", is_staff=True)

    def test_picker_requires_login(self):
        self.assertIn("/login/", Client().get("/").url)

    def test_proxy_requires_login(self):
        self.assertIn("/login/", Client().get("/d/legacy/").url)

    def test_proxy_subpath_requires_login(self):
        self.assertIn("/login/", Client().get("/d/legacy/api/state?range=24h").url)

    def test_disabled_dashboard_is_404_even_when_signed_in(self):
        c = Client()
        c.force_login(self.user)
        self.assertEqual(c.get("/d/hidden/").status_code, 404)

    def test_unreachable_upstream_renders_the_offline_page(self):
        c = Client()
        c.force_login(self.user)
        # Mocked rather than relying on port 8000 being free — navdash itself
        # may well be running on this machine while the suite executes.
        with mock.patch("gateway.views.requests.request",
                        side_effect=requests.ConnectionError("refused")):
            response = c.get("/d/legacy/")
        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "Legacy Tracker", status_code=502)

    def test_cross_origin_write_is_rejected(self):
        c = Client()
        c.force_login(self.user)
        response = c.post("/d/legacy/api/config", data="{}", content_type="application/json",
                          HTTP_ORIGIN="http://evil.example")
        self.assertEqual(response.status_code, 403)

    def test_user_management_is_staff_only(self):
        c = Client()
        c.force_login(self.user)
        self.assertEqual(c.get("/users/").status_code, 302)
        c.force_login(self.staff)
        self.assertEqual(c.get("/users/").status_code, 200)

    def test_no_dashboard_api_path_is_reachable_anonymously(self):
        """Guards against a URL pattern that accidentally skips @login_required."""
        anon = Client()
        for path in ("/d/legacy/", "/d/legacy/api/state", "/d/legacy/partials/catalog/",
                     "/d/legacy/static/app.js", "/d/legacy/a/b/c/d.json",
                     "/api/status/"):
            response = anon.get(path)
            self.assertEqual(response.status_code, 302, f"{path} was not gated")
            self.assertIn("/login/", response.url, path)

    def test_admin_cannot_deactivate_themselves(self):
        c = Client()
        c.force_login(self.staff)
        response = c.post(f"/users/{self.staff.pk}/", {
            "username": "boss", "first_name": "", "last_name": "",
            "email": "boss@example.com", "is_staff": "on",
        })
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)
        self.assertContains(response, "cannot deactivate your own account")