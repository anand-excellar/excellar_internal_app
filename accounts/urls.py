from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts import views

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password/", views.change_own_password, name="change-own-password"),
    path("users/", views.user_list, name="user-list"),
    path("users/new/", views.user_create, name="user-create"),
    path("users/<int:pk>/", views.user_edit, name="user-edit"),
    path("users/<int:pk>/password/", views.user_password, name="user-password"),
    path("users/<int:pk>/delete/", views.user_delete, name="user-delete"),
]