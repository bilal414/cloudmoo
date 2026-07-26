from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
import uuid

from ..utils.recaptcha import verify_recaptcha


class SignupForm(UserCreationForm):
    recaptcha_token = forms.CharField(widget=forms.HiddenInput(), required=False)

    first_name = forms.CharField(
        max_length=30,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:bg-white focus:outline-hidden focus:ring-indigo-500 sm:text-sm',
            'autocomplete': 'given-name'
        })
    )

    last_name = forms.CharField(
        max_length=30,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:bg-white focus:outline-hidden focus:ring-indigo-500 sm:text-sm',
            'autocomplete': 'family-name'
        })
    )

    email = forms.EmailField(
        max_length=254,
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:bg-white focus:outline-hidden focus:ring-indigo-500 sm:text-sm',
            'autocomplete': 'email'
        })
    )

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['password1'].widget = forms.PasswordInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:bg-white focus:outline-hidden focus:ring-indigo-500 sm:text-sm',
            'autocomplete': 'new-password'
        })
        self.fields['password2'].widget = forms.PasswordInput(attrs={
            'class': 'block w-full appearance-none rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:bg-white focus:outline-hidden focus:ring-indigo-500 sm:text-sm',
            'autocomplete': 'new-password'
        })
        if 'username' in self.fields:
            del self.fields['username']

    def clean_password1(self):
        password1 = self.cleaned_data.get('password1')
        try:
            validate_password(password1, self.instance)
        except ValidationError as error:
            self.add_error('password1', error)
        return password1

    def clean_password2(self):
        password1 = self.cleaned_data.get('password1')
        password2 = self.cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            raise ValidationError("Passwords don't match")
        return password2

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def generate_username(self):
        max_attempts = 10
        for _ in range(max_attempts):
            username = str(uuid.uuid4())[:30]
            if not User.objects.filter(username=username).exists():
                return username
        raise ValidationError("Unable to generate a unique username. Please try again.")

    def clean_recaptcha_token(self):
        token = self.cleaned_data.get('recaptcha_token')
        success, error = verify_recaptcha(token)
        if not success:
            raise ValidationError(error)
        return token

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.generate_username()
        user.email = self.cleaned_data["email"]
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        if commit:
            user.save()
        return user
