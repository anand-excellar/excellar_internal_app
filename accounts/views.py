from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import AdminPasswordChangeForm, PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.forms import INPUT, PortalUserChangeForm, PortalUserCreationForm

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", INPUT)
        return form


@login_required
@staff_required
def user_list(request):
    return render(request, "accounts/user_list.html", {
        "users": User.objects.order_by("-is_staff", "username"),
    })


@login_required
@staff_required
def user_create(request):
    form = PortalUserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, f"Created {user.username}.")
        return redirect("user-list")
    return render(request, "accounts/user_form.html", {
        "form": form, "title": "New user", "submit": "Create user",
    })


@login_required
@staff_required
def user_edit(request, pk: int):
    user = get_object_or_404(User, pk=pk)
    form = PortalUserChangeForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        # Locking yourself out is the one edit that cannot be undone from the UI.
        if user == request.user and not form.cleaned_data["is_active"]:
            form.add_error("is_active", "You cannot deactivate your own account.")
        elif user == request.user and not form.cleaned_data["is_staff"]:
            form.add_error("is_staff", "You cannot remove your own administrator rights.")
        else:
            form.save()
            messages.success(request, f"Updated {user.username}.")
            return redirect("user-list")
    return render(request, "accounts/user_form.html", {
        "form": form, "title": f"Edit {user.username}", "submit": "Save changes",
        "target": user,
    })


@login_required
@staff_required
def user_password(request, pk: int):
    user = get_object_or_404(User, pk=pk)
    form = AdminPasswordChangeForm(user, request.POST or None)
    for field in form.fields.values():
        field.widget.attrs.setdefault("class", INPUT)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Password reset for {user.username}.")
        return redirect("user-list")
    return render(request, "accounts/user_form.html", {
        "form": form, "title": f"Set password for {user.username}",
        "submit": "Set password", "target": user,
    })


@login_required
@staff_required
def user_delete(request, pk: int):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user-list")
    if request.method == "POST":
        username = user.username
        user.delete()
        messages.success(request, f"Deleted {username}.")
        return redirect("user-list")
    return render(request, "accounts/user_confirm_delete.html", {"target": user})


@login_required
def change_own_password(request):
    """Any signed-in user can rotate their own password."""
    form = PasswordChangeForm(request.user, request.POST or None)
    for field in form.fields.values():
        field.widget.attrs.setdefault("class", INPUT)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        # Rotating a password cycles the session hash; without this the user is
        # signed straight back out again.
        from django.contrib.auth import update_session_auth_hash
        update_session_auth_hash(request, user)
        messages.success(request, "Your password has been changed.")
        return redirect("picker")
    return render(request, "accounts/user_form.html", {
        "form": form, "title": "Change your password", "submit": "Change password",
        "cancel_url": reverse("picker"),
    })