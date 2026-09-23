from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic.base import RedirectView

urlpatterns = [
    # Browsers request /favicon.ico at the origin root even while viewing a
    # proxied dashboard, so answering here keeps a 404 out of every dashboard's
    # console — and puts the Excellar mark on the tab.
    path("favicon.ico", RedirectView.as_view(
        url=static("img/favicon.ico"), permanent=True)),
    path("admin/", admin.site.urls),
    path("", include("catalog.urls")),
    path("", include("accounts.urls")),

    # Dashboards that live in this project. These must come before the gateway,
    # whose pattern would otherwise swallow every /d/<slug>/ path.
    path("d/nav/", include("nav.urls")),
    path("d/callspread/", include("callspread.urls")),

    # NAV's REST API, unchanged from the standalone project.
    path("api/nav/", include("api.urls")),

    # Catch-all for dashboards that are separate services (Dashboard.kind =
    # "proxied"). Nothing uses it today; it is what lets a non-Python dashboard
    # be added later without touching this project.
    path("d/", include("gateway.urls")),
]