"""HTTP surface for the CallSpread dashboard.

The JSON endpoints answer exactly what the original Node server did, because
static/callspread/app.js is the original file and consumes these shapes.
"""

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from callspread import config as config_module
from callspread.runtime import get_runtime

MOUNT = "/d/callspread"


def _json(payload, status=200):
    # The browser code expects plain JSON objects and arrays, not Django's
    # safe-mode wrapping, and must never see a cached response.
    response = JsonResponse(payload, safe=False, status=status,
                            json_dumps_params={"allow_nan": False})
    response["Cache-Control"] = "no-store"
    return response


@login_required
def index(request):
    return render(request, "callspread/index.html", {"mount": MOUNT})


def _state_payload(runtime, range_key):
    state = runtime.read_state()
    # The poller writes history from its own process, so re-read before serving.
    runtime.store.reload()
    return {
        "mode": runtime.mode,
        "snapshot": state.get("snapshot"),
        "history": runtime.store.range(range_key, 400),
        "rules": state.get("rules", []),
        "range": range_key,
        "config": runtime.config,
        "lastPollAt": state.get("lastPollAt"),
        "lastError": state.get("lastError"),
        "pollSeconds": runtime.config["polling"]["intervalSeconds"],
    }


@login_required
def state(request):
    runtime = get_runtime()
    range_key = request.GET.get("range") or "24h"
    payload = _state_payload(runtime, range_key)
    # Nothing on disk yet (first boot before the poller has run) — do one poll
    # inline so the dashboard opens with data instead of an empty shell.
    if payload["snapshot"] is None:
        runtime.poll()
        payload = _state_payload(runtime, range_key)
    return _json(payload)


@login_required
def history(request):
    runtime = get_runtime()
    range_key = request.GET.get("range") or "24h"
    runtime.store.reload()
    return _json({"range": range_key, "points": runtime.store.range(range_key, 400)})


@login_required
def snapshot(request):
    return _json(get_runtime().read_state().get("snapshot"))


@login_required
@require_http_methods(["POST"])
def refresh(request):
    runtime = get_runtime()
    snap = runtime.poll()
    return _json({"snapshot": snap, "lastError": runtime.last_error})


@login_required
@require_http_methods(["POST"])
def update_config(request):
    runtime = get_runtime()
    try:
        patch = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return _json({"error": f"invalid JSON: {exc}"}, status=400)

    config_module.save(config_module.apply_patch(runtime.reload_config(), patch))
    runtime.reload_config()
    # Recompute without polluting history — the thresholds changed, not the book.
    snap = runtime.poll(persist=False)
    return _json({"config": runtime.config, "snapshot": snap})


@login_required
def health(request):
    runtime = get_runtime()
    state_data = runtime.read_state()
    runtime.store.reload()
    return _json({
        "ok": not state_data.get("lastError"),
        "mode": runtime.mode,
        "lastPollAt": state_data.get("lastPollAt"),
        "points": len(runtime.store.points),
        "lastError": state_data.get("lastError"),
    })