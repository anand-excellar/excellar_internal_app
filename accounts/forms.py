from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

# Classes defined in static/css/portal.css.
INPUT = "input"
CHECKBOX = "checkbox"


def _style(fields):
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", CHECKBOX)
        else:
            widget.attrs.setdefault("class", INPUT)


class PortalUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
        _style(self.fields)


class PortalUserChangeForm(forms.ModelForm):
    """Edit an existing account.

    Password is deliberately absent — it is changed through its own screen so a
    routine profile edit can never blank or reset a credential by accident.
    """

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "is_active", "is_staff")
        labels = {
            "is_active": "Active (can sign in)",
            "is_staff": "Administrator (can manage users)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
        _style(self.fields)