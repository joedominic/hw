"""Authentication forms."""
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from .account import email_taken, normalize_email

User = get_user_model()


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={"autocomplete": "email"}))

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (
                f"{css} w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm "
                f"text-slate-900 shadow-sm outline-none transition "
                f"placeholder:text-slate-400 focus:border-brand focus:ring-2 focus:ring-brand/20"
            ).strip()

    def clean_email(self):
        email = normalize_email(self.cleaned_data.get("email") or "")
        if not email:
            raise ValidationError("Email is required.")
        if email_taken(email):
            raise ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (
                f"{css} w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm "
                f"text-slate-900 shadow-sm outline-none transition "
                f"placeholder:text-slate-400 focus:border-brand focus:ring-2 focus:ring-brand/20"
            ).strip()
