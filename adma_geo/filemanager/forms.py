from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model
from .models import Folder, File

User = get_user_model()

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result

class RegistrationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=30, required=True)
    last_name = forms.CharField(max_length=150, required=True)

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'password1', 'password2')

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email=email).exists():
            # Use a generic message to avoid disclosing whether the email
            # is registered (account enumeration via registration form).
            raise forms.ValidationError(
                "Unable to create an account with this email address. "
                "Please use a different email or contact support."
            )
        return email

class ProfileForm(forms.ModelForm):
    """
    Edit the parts of a user's account they own.

    username is deliberately absent: it is the login identifier for password
    accounts, so renaming would lock people out of their own credentials.

    email is editable only for password accounts. For accounts that sign in
    through a social provider it is rendered read-only, because the address
    is supplied and verified by that provider — letting it drift here would
    leave the page showing one address while the user signs in with another,
    and would desynchronise allauth's own EmailAddress record.
    """

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email')

    def __init__(self, *args, email_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.email_locked = email_locked
        if email_locked:
            self.fields['email'].disabled = True
            self.fields['email'].help_text = (
                'Provided by the account you sign in with, so it cannot be '
                'changed here.'
            )

    def clean_email(self):
        email = self.cleaned_data.get('email')
        # A disabled field always returns its initial value, so there is
        # nothing to re-validate for social accounts.
        if self.email_locked:
            return self.instance.email
        if email and User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            # RegistrationForm rejects duplicate addresses; without the same
            # check here a profile edit would be a way around it.
            raise forms.ValidationError(
                "Unable to use this email address. Please use a different one."
            )
        return email


class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ['name', 'is_public']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter folder name'
            }),
            'is_public': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            })
        }

class FileUploadForm(forms.Form):
    files = MultipleFileField(required=True)
    is_public = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input'
        })
    )
