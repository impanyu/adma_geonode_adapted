"""
Registration collects no email address, and Google sign-in is therefore
always a one-click affair.

The two are the same fact seen from either end. allauth refuses to auto-create
an account when an existing user already claims the incoming address; it
bounces the visitor to /accounts/3rdparty/signup/, a form they cannot complete
because it re-checks the same uniqueness rule. Keeping addresses out of the
registration form removes the only way that collision could arise.
"""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from allauth.account.models import EmailAddress
from allauth.socialaccount.internal.flows.signup import process_auto_signup
from allauth.socialaccount.models import SocialAccount, SocialLogin

from filemanager.forms import ProfileForm, RegistrationForm

User = get_user_model()


def google_login(email, verified=True):
    """A SocialLogin shaped the way allauth's Google provider builds one."""
    return SocialLogin(
        user=User(email=email),
        account=SocialAccount(provider='google', uid='uid-' + email),
        email_addresses=[
            EmailAddress(email=email, verified=verified, primary=True)
        ],
    )


class RegistrationFormTests(TestCase):
    def test_no_email_field(self):
        self.assertNotIn('email', RegistrationForm().fields)

    def test_registers_without_an_email(self):
        form = RegistrationForm(data={
            'username': 'grower',
            'first_name': 'Pat',
            'last_name': 'Nguyen',
            'password1': 'correct-horse-battery',
            'password2': 'correct-horse-battery',
        })
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, '')

    def test_two_accounts_can_coexist_without_emails(self):
        """The old unique-email check would have had nothing to compare here."""
        for name in ('first', 'second'):
            form = RegistrationForm(data={
                'username': name,
                'first_name': 'A',
                'last_name': 'B',
                'password1': 'correct-horse-battery',
                'password2': 'correct-horse-battery',
            })
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        self.assertEqual(User.objects.filter(email='').count(), 2)


class ProfileFormTests(TestCase):
    def test_email_is_not_editable(self):
        self.assertNotIn('email', ProfileForm().fields)

    def test_saving_leaves_a_provider_address_alone(self):
        user = User.objects.create_user('grower', password='x')
        user.email = 'grower@gmail.com'
        user.save()

        form = ProfileForm(
            data={'first_name': 'Pat', 'last_name': 'Nguyen'}, instance=user
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        user.refresh_from_db()
        self.assertEqual(user.email, 'grower@gmail.com')
        self.assertEqual(user.first_name, 'Pat')


class GoogleAutoSignupTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get('/')

    def test_new_visitor_signs_in_without_a_form(self):
        allowed, response = process_auto_signup(
            self.request, google_login('brand.new@gmail.com')
        )
        self.assertTrue(allowed)
        self.assertIsNone(response)

    def test_password_accounts_never_claim_an_address(self):
        """
        A password account registered with the same local-part holds no email,
        so it cannot collide with the Google visitor.
        """
        form = RegistrationForm(data={
            'username': 'brand.new',
            'first_name': 'A',
            'last_name': 'B',
            'password1': 'correct-horse-battery',
            'password2': 'correct-horse-battery',
        })
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        allowed, response = process_auto_signup(
            self.request, google_login('brand.new@gmail.com')
        )
        self.assertTrue(allowed)
        self.assertIsNone(response)

    def test_a_taken_address_still_blocks_auto_signup(self):
        """
        The guard itself is untouched: an account that does hold the address --
        one created before this change, or one linked from a provider -- still
        stops a second account being auto-created for it. Those users connect
        Google from their profile page instead.
        """
        user = User.objects.create_user('existing', password='x')
        user.email = 'taken@gmail.com'
        user.save()

        allowed, _ = process_auto_signup(
            self.request, google_login('taken@gmail.com')
        )
        self.assertFalse(allowed)
