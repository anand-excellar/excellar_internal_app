from django.urls import path, re_path

from gateway import views

urlpatterns = [
    path("<slug:slug>/", views.proxy, name="dashboard-proxy-root"),
    re_path(r"^(?P<slug>[-\w]+)/(?P<subpath>.*)$", views.proxy, name="dashboard-proxy"),
]