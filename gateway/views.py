import logging
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt

from catalog.models import Dashboard
from gateway.rewrite import inject_base_and_shim, rewrite_location, rewrite_set_cookie

log = logging.getLogger(__name__)

# Per RFC 7230 these describe a single hop and must not be forwarded.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# Set by the proxy itself; copying the client's values would corrupt the request.
SKIP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}

SKIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "content-length", "set-cookie"}

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

STREAM_CHUNK = 64 * 1024


def _client_headers(request) -> dict:
    """Rebuild the inbound headers for the upstream request."""
    headers = {}
    for key, value in request.META.items():
        if not key.startswith("HTTP_"):
            continue
        name = key[5:].replace("_", "-").lower()
        if name in SKIP_REQUEST_HEADERS:
            continue
        headers[name] = value

    if content_type := request.META.get("CONTENT_TYPE"):
        headers["content-type"] = content_type

    # The upstream must not see the portal's session — a dashboard has no
    # business holding a credential for the front door.
    if cookie := _strip_portal_cookies(request.META.get("HTTP_COOKIE", "")):
        headers["cookie"] = cookie
    else:
        headers.pop("cookie", None)

    # Identity encoding keeps HTML injectable without a decompress round-trip;
    # on loopback the saving from gzip is not worth the complexity.
    headers["accept-encoding"] = "identity"

    headers["x-forwarded-for"] = request.META.get("REMOTE_ADDR", "")
    headers["x-forwarded-proto"] = "https" if request.is_secure() else "http"
    headers["x-forwarded-host"] = request.get_host()
    # Advertised for upstreams that know how to honour a mount prefix; both
    # current dashboards ignore it and are handled by the injected shim instead.
    headers["x-forwarded-user"] = request.user.get_username()
    return headers


def _strip_portal_cookies(raw: str) -> str:
    keep = []
    for part in raw.split(";"):
        name = part.split("=", 1)[0].strip()
        if name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
            continue
        if part.strip():
            keep.append(part.strip())
    return "; ".join(keep)


def _same_origin(request) -> bool:
    """Cheap CSRF stand-in for proxied writes.

    The proxy is csrf_exempt because the upstream owns its own token scheme and
    we must not consume or rewrite it. Requiring Origin/Referer to match the
    portal's host restores the protection Django's middleware would have given.
    """
    origin = request.META.get("HTTP_ORIGIN")
    if origin:
        return urlsplit(origin).netloc == request.get_host()
    referer = request.META.get("HTTP_REFERER")
    if referer:
        return urlsplit(referer).netloc == request.get_host()
    return False


@csrf_exempt
@login_required
def proxy(request, slug: str, subpath: str = ""):
    dashboard = get_object_or_404(Dashboard, slug=slug, enabled=True)

    if request.method in UNSAFE_METHODS and not _same_origin(request):
        return HttpResponse("Cross-origin write rejected.", status=403)

    mount = dashboard.mount
    url = dashboard.upstream_root + "/" + quote(subpath)
    if query := request.META.get("QUERY_STRING"):
        url = f"{url}?{query}"

    try:
        upstream = requests.request(
            method=request.method,
            url=url,
            headers=_client_headers(request),
            data=request.body if request.method in UNSAFE_METHODS or request.body else None,
            allow_redirects=False,
            stream=True,
            timeout=(settings.GATEWAY_CONNECT_TIMEOUT, settings.GATEWAY_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        log.warning("gateway: %s unreachable at %s (%s)", slug, dashboard.upstream_root, exc)
        return render(request, "gateway/upstream_down.html", {
            "dashboard": dashboard,
            "error": str(exc),
        }, status=502)

    content_type = upstream.headers.get("Content-Type", "")
    is_html = "text/html" in content_type.lower()

    if is_html:
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        body = inject_base_and_shim(upstream.content, mount, charset)
        response = HttpResponse(body, status=upstream.status_code, content_type=content_type)
    else:
        response = StreamingHttpResponse(
            upstream.iter_content(chunk_size=STREAM_CHUNK),
            status=upstream.status_code,
            content_type=content_type or "application/octet-stream",
        )

    for name, value in upstream.headers.items():
        lowered = name.lower()
        if lowered in SKIP_RESPONSE_HEADERS or lowered == "content-type":
            continue
        if lowered == "location":
            response[name] = rewrite_location(value, mount, dashboard.upstream_root)
        else:
            response[name] = value

    _forward_cookies(response, upstream, mount, slug)

    return response


def _forward_cookies(response, upstream, mount: str, slug: str):
    """Pass the upstream's Set-Cookie headers through, re-scoped to the mount.

    A response can carry several, and Django emits one line per entry in its
    cookie jar, so the jar — not the header dict — is the place to put them.
    """
    jar = SimpleCookie()
    for raw in upstream.raw.headers.getlist("Set-Cookie"):
        try:
            jar.load(rewrite_set_cookie(raw, mount))
        except CookieError:
            log.warning("gateway: %s sent an unparseable Set-Cookie, dropped", slug)
    response.cookies.update(jar)