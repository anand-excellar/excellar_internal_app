"""Browser check for the parts of the gateway that only exist at runtime.

The Django suite (`manage.py test`) covers the rewriting functions directly, but
the injected shim is client-side JavaScript: whether htmx's XHR and CallSpread's
fetch actually land under /d/<slug>/ can only be proven by a real browser.

    pip install -r requirements-dev.txt
    python tests/e2e_browser.py --password <admin password>

Requires the portal on :8080 and whichever dashboards you want exercised to be
running. Dashboards that are down are skipped, not failed.
"""

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--base", default=os.environ.get("PORTAL_BASE", "http://127.0.0.1:8080"))
parser.add_argument("--username", default=os.environ.get("PORTAL_USER", "admin"))
parser.add_argument("--password", default=os.environ.get("PORTAL_PASSWORD"))
parser.add_argument(
    "--chrome", default=os.environ.get("CHROME_PATH"),
    help="Path to a Chromium binary. Omit to use playwright's own download.")
args = parser.parse_args()

if not args.password:
    parser.error("--password (or PORTAL_PASSWORD) is required")

BASE = args.base.rstrip("/")
failures, requests_seen = [], []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


with sync_playwright() as p:
    launch = {"executable_path": args.chrome} if args.chrome else {}
    browser = p.chromium.launch(**launch)
    page = browser.new_page()
    page.on("request", lambda r: requests_seen.append(r.url))

    # --- the gate ------------------------------------------------------------
    page.goto(f"{BASE}/d/callspread/", wait_until="domcontentloaded")
    check("anonymous dashboard access redirects to login", "/login/" in page.url, page.url)

    page.goto(f"{BASE}/login/", wait_until="domcontentloaded")
    page.fill("input[name=username]", args.username)
    page.fill("input[name=password]", args.password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    check("login lands on the picker", page.url.rstrip("/") == BASE, page.url)
    if failures:
        browser.close()
        sys.exit("cannot continue without a session")

    # --- picker --------------------------------------------------------------
    page.wait_for_timeout(2500)  # the status probe is async by design
    live = page.eval_on_selector_all(
        ".status-badge",
        "els => els.filter(e => e.textContent.trim() === 'live').map(e => e.dataset.slug)")
    print(f"   dashboards reporting live: {live or 'none'}")
    check("picker rendered at least one dashboard",
          bool(page.query_selector_all(".status-badge")))

    # --- each live dashboard's own requests must stay under its mount --------
    for slug in live:
        requests_seen.clear()
        page.goto(f"{BASE}/d/{slug}/", wait_until="networkidle")
        page.wait_for_timeout(2500)

        own = [u for u in requests_seen if u.startswith(BASE)]
        # /static/ and /favicon.ico are served at the origin root for the whole
        # project, so being outside the mount is correct for those. What must
        # never escape is a data request: an /api/ call landing at the root would
        # mean the dashboard is reading someone else's endpoint.
        allowed = (f"/d/{slug}/", "/static/", "/favicon")
        escaped = [u for u in own if not any(part in u for part in allowed)]
        check(f"{slug}: no data request escaped the mount", not escaped, str(escaped[:4]))
        check(f"{slug}: made requests under its mount",
              any(f"/d/{slug}/" in u for u in own), "no mounted requests seen")

    # --- the session survived every proxied app's cookies --------------------
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    check("still signed in after visiting the dashboards", "/login/" not in page.url, page.url)

    browser.close()

print()
if failures:
    sys.exit(f"{len(failures)} FAILED: {failures}")
print("all checks passed")