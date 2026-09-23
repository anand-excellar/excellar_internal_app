from django.urls import path
from nav import views

urlpatterns = [
    path("", views.index, name="nav-index"),
    path("partials/snapshots/", views.partial_snapshots, name="nav-snapshots"),
    path("partials/catalog/", views.partial_catalog, name="nav-catalog"),
    path("partials/bitgo-holdings/", views.partial_bitgo_holdings, name="nav-bitgo-holdings"),
    path("partials/portfolio/", views.partial_portfolio, name="nav-portfolio"),
    path("chart-data/", views.chart_data, name="nav-chart-data"),
    path("partials/pnl-debug/", views.partial_pnl_debug, name="nav-pnl-debug"),
]
