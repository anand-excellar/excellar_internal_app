from django.urls import path

from callspread import views

# Paths mirror the original Node server exactly — static/callspread/app.js is
# the unchanged original and calls these by name.
urlpatterns = [
    path("", views.index, name="callspread-index"),
    path("api/state", views.state, name="callspread-state"),
    path("api/history", views.history, name="callspread-history"),
    path("api/snapshot", views.snapshot, name="callspread-snapshot"),
    path("api/refresh", views.refresh, name="callspread-refresh"),
    path("api/config", views.update_config, name="callspread-config"),
    path("api/health", views.health, name="callspread-health"),
]