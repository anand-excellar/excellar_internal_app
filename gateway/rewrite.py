"""Making an unmodified app work when it is served under /d/<slug>/.

Neither dashboard knows it is mounted under a prefix, and we do not want to
patch them — navdash emits root-absolute htmx URLs from `{% url %}`, CallSpread
fetches `/api/state` and loads `styles.css` relatively. Rather than rewriting
their source, the proxy injects two things into every HTML page it serves:

  1. a <base> tag, so relative asset URLs resolve under the mount, and
  2. a small shim that prefixes root-absolute, same-origin URLs used by
     fetch/XHR (htmx uses XHR) with the mount.

Everything else about the upstream response passes through untouched.
"""

import re

_HEAD_RE = re.compile(rb"<head[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(rb"<html[^>]*>", re.IGNORECASE)

_SHIM = """
<script>
(function () {
  var P = "__MOUNT__";
  function fix(u) {
    if (typeof u !== "string" || !u) return u;
    if (u.charAt(0) === "#") return u;
    if (/^[a-z][a-z0-9+.\\-]*:/i.test(u) || u.slice(0, 2) === "//") {
      try {
        var a = new URL(u, location.href);
        if (a.origin !== location.origin) return u;
        if (a.pathname === P || a.pathname.indexOf(P + "/") === 0) return u;
        return P + a.pathname + a.search + a.hash;
      } catch (e) { return u; }
    }
    if (u.charAt(0) !== "/") return u;          // relative — <base> handles it
    if (u === P || u.indexOf(P + "/") === 0) return u;
    return P + u;
  }
  var of = window.fetch;
  if (of) {
    window.fetch = function (input, init) {
      if (typeof input === "string") input = fix(input);
      else if (typeof Request !== "undefined" && input instanceof Request) {
        var f = fix(input.url);
        if (f !== input.url) input = new Request(f, input);
      }
      return of.call(this, input, init);
    };
  }
  var oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var args = Array.prototype.slice.call(arguments);
    args[1] = fix(url);
    return oo.apply(this, args);
  };
  if (window.EventSource) {
    var OE = window.EventSource;
    window.EventSource = function (url, cfg) { return new OE(fix(url), cfg); };
    window.EventSource.prototype = OE.prototype;
  }
})();
</script>
"""


def inject_base_and_shim(body: bytes, mount: str, charset: str = "utf-8") -> bytes:
    """Insert <base> + the URL shim as early in <head> as possible.

    They must precede the page's own scripts and stylesheet links, otherwise
    those resolve against the wrong root before the shim installs.
    """
    prelude = (
        f'<base href="{mount}/">' + _SHIM.replace("__MOUNT__", mount)
    ).encode(charset, "replace")

    match = _HEAD_RE.search(body)
    if match:
        return body[: match.end()] + prelude + body[match.end():]

    match = _HTML_RE.search(body)
    if match:
        return body[: match.end()] + b"<head>" + prelude + b"</head>" + body[match.end():]

    return prelude + body


_PATH_ATTR_RE = re.compile(r"(;\s*)(?i:path)\s*=\s*([^;]*)")


def rewrite_set_cookie(value: str, mount: str) -> str:
    """Pin an upstream cookie to this dashboard's mount.

    navdash is Django too and sets `sessionid`/`csrftoken`. Left at Path=/ those
    would ride along on portal requests. Confining them to /d/<slug>/ keeps each
    dashboard's cookie jar to itself. (The portal's own cookies are separately
    renamed in settings, so even a name clash cannot log the user out.)
    """
    scoped = f"{mount}/"
    if _PATH_ATTR_RE.search(value):
        def _replace(match):
            upstream_path = match.group(2).strip() or "/"
            return f"{match.group(1)}Path={scoped}{upstream_path.lstrip('/')}"
        return _PATH_ATTR_RE.sub(_replace, value, count=1)
    return f"{value}; Path={scoped}"


def rewrite_location(value: str, mount: str, upstream_root: str) -> str:
    """Keep an upstream redirect inside the mount instead of escaping to /."""
    if value.startswith(upstream_root):
        value = value[len(upstream_root):] or "/"
    if value.startswith("/") and not value.startswith("//"):
        if value == mount or value.startswith(mount + "/"):
            return value
        return mount + value
    return value