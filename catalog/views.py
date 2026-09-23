from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from catalog.models import Dashboard


@login_required
def picker(request):
    """The list of dashboards a signed-in user chooses from."""
    return render(request, "catalog/picker.html", {
        "dashboards": Dashboard.objects.filter(enabled=True),
    })


def _probe(dash: Dashboard) -> tuple[str, bool]:
    # An internal dashboard runs in this very process — if it were down, nothing
    # would have served the page asking the question.
    if dash.is_internal:
        return dash.slug, True
    url = dash.upstream_root + "/" + (dash.health_path or "/").lstrip("/")
    try:
        resp = requests.get(
            url,
            timeout=settings.GATEWAY_PROBE_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        resp.close()
        # Any HTTP answer means the process is listening and healthy enough to
        # route to; only 5xx is treated as down.
        return dash.slug, resp.status_code < 500
    except requests.RequestException:
        return dash.slug, False


@login_required
def status(request):
    """Live/offline badges for the picker, fetched after page load.

    Probing on render would make the picker as slow as the slowest dashboard,
    so this is a separate call the page fills in asynchronously.
    """
    dashboards = list(Dashboard.objects.filter(enabled=True))
    if not dashboards:
        return JsonResponse({})
    with ThreadPoolExecutor(max_workers=min(8, len(dashboards))) as pool:
        results = dict(pool.map(_probe, dashboards))
    return JsonResponse(results)