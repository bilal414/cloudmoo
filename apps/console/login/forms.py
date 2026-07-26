from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import ValidationError

from ..utils.recaptcha import verify_recaptcha


class LoginForm(forms.Form):
    email = forms.EmailField(
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:bg-white focus:outline-hidden focus:ring-blue-500 sm:text-sm',
            'autocomplete': 'email'
        })
    )

    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:bg-white focus:outline-hidden focus:ring-blue-500 sm:text-sm',
            'autocomplete': 'password'
        })
    )

    recaptcha_token = forms.CharField(required=False, widget=forms.HiddenInput())

    def clean_recaptcha_token(self):
        token = self.cleaned_data.get('recaptcha_token')
        success, error = verify_recaptcha(token)
        if not success:
            raise ValidationError(error)
        return token

class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:bg-white focus:outline-hidden focus:ring-blue-500 sm:text-sm',
            'autocomplete': 'email'
        })
    )
    recaptcha_token = forms.CharField(required=False, widget=forms.HiddenInput())

    def clean_recaptcha_token(self):
        token = self.cleaned_data.get('recaptcha_token')
        success, error = verify_recaptcha(token)
        if not success:
            raise ValidationError(error)
        return token

class PasswordResetConfirmForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].widget.attrs.update({
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:bg-white focus:outline-hidden focus:ring-blue-500 sm:text-sm'
        })
        self.fields['new_password2'].widget.attrs.update({
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:bg-white focus:outline-hidden focus:ring-blue-500 sm:text-sm'
        })

class VerifyTwoFactorForm(forms.Form):
    code = forms.CharField(
        widget=forms.TextInput(
            attrs={
                'class': 'appearance-none block w-full px-3 py-2 border border-gray-300 rounded-md',
                'placeholder': 'Enter 6-digit code'
            }
        ),
        max_length=6,
        min_length=6
    )