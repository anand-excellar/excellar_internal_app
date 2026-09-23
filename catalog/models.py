from django.db import models


class Dashboard(models.Model):
    """A dashboard the portal offers, and how it is served.

    Two kinds:

    * ``internal`` — a Django app inside this project (NAV, CallSpread). Served
      directly at ``/d/<slug>/`` by that app's own urls.py. Nothing to start,
      nothing to proxy; it is the same process.
    * ``proxied``  — a separate program listening on loopback (any language).
      The gateway forwards to ``upstream`` after checking the session.

    Everything is internal today. ``proxied`` stays available so a future
    dashboard that isn't Python can be added without changing this project.
    """

    INTERNAL = "internal"
    PROXIED = "proxied"
    KIND_CHOICES = [
        (INTERNAL, "Internal — a Django app in this project"),
        (PROXIED, "Proxied — a separate service on loopback"),
    ]

    slug = models.SlugField(
        max_length=40, unique=True,
        help_text="URL segment — the dashboard is served at /d/<slug>/",
    )
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    icon = models.CharField(
        max_length=8, blank=True, default="",
        help_text="A single emoji shown on the picker card.",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=INTERNAL)
    upstream = models.URLField(
        max_length=300, blank=True, default="",
        help_text="Proxied dashboards only — the loopback origin it listens on, "
                  "e.g. http://127.0.0.1:4310. Leave blank for internal ones.",
    )
    health_path = models.CharField(
        max_length=200, default="/", blank=True,
        help_text="Proxied dashboards only — path probed for the live/offline badge.",
    )
    enabled = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    @property
    def mount(self) -> str:
        """Prefix the dashboard is served under, with no trailing slash."""
        return f"/d/{self.slug}"

    @property
    def path(self) -> str:
        return f"{self.mount}/"

    @property
    def is_internal(self) -> bool:
        return self.kind == self.INTERNAL

    @property
    def upstream_root(self) -> str:
        return self.upstream.rstrip("/")