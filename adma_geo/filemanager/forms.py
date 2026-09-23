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
    """
    Sign up with a username and password only.

    No email address is collected. The site cannot send mail (there is no SMTP
    host configured), so an address gathered here could never be verified or
    used for password recovery -- it was an unverified string that nothing
    relied on. Worse, it collided with Google sign-in: allauth refuses to
    auto-create an account when some existing user already claims that
    address, so anyone whose address had been typed into this form was
    bounced to a signup page they could not complete.

    Leaving the field out means an email address only ever reaches ADMA from a
    provider that verified it. See ProfileForm.
    """

    first_name = forms.CharField(max_length=30, required=True)
    last_name = forms.CharField(max_length=150, required=True)

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'password1', 'password2')

class ProfileForm(forms.ModelForm):
    """
    Edit the parts of a user's account they own.

    username is deliberately absent: it is the login identifier for password
    accounts, so renaming would lock people out of their own credentials.

    email is absent too. An address only ever reaches ADMA from a provider
    that verified it (see RegistrationForm), so there is nothing here for the
    user to type: editing it would leave the page showing one address while
    they sign in with another, and would desynchronise allauth's own
    EmailAddress record. The profile page displays the provider's address
    read-only instead.
    """

    class Meta:
        model = User
        fields = ('first_name', 'last_name')


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
