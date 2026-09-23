from django.urls import path

from catalog import views

urlpatterns = [
    path("", views.picker, name="picker"),
    path("api/status/", views.status, name="dashboard-status"),
]