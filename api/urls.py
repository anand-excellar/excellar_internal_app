from django.urls import path
from api import views

urlpatterns = [
    path("snapshots/", views.SnapshotListView.as_view()),
    path("snapshots/latest/", views.SnapshotLatestView.as_view()),
    path("snapshots/<int:pk>/", views.SnapshotDetailView.as_view()),
    path("daily-pnl/", views.DailyPnlListView.as_view()),
    path("daily-pnl/export/", views.DailyPnlExportView.as_view()),
    path("daily-pnl/<str:segment>/<str:date>/", views.DailyPnlDetailView.as_view()),
    path("cross-mtm-config/", views.CrossMtmConfigListView.as_view()),
    path("portfolio/summary/", views.PortfolioSummaryView.as_view()),
    path("actions/snapshot/", views.TriggerSnapshotView.as_view()),
    path("actions/finalize/", views.TriggerFinalizeView.as_view()),
]
