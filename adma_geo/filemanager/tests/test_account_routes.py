"""
Which account URLs this site actually serves.

Two url sets are mounted under /accounts/: contrib.auth's and allauth's. Both
ship a password reset that needs to send mail, and allauth also ships a signup
form of its own. With no SMTP host and no email collected at registration,
neither reset can ever work, and the second signup form is a way around the
registration form -- it asks for the address we stopped collecting.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class AccountRouteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')

    def test_sign_in_still_works(self):
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 200)

        self.assertTrue(
            self.client.login(username='grower', password='correct-horse-battery')
        )

    def test_neither_password_reset_offers_a_form(self):
        """contrib.auth's is gone; allauth's explains instead of pretending."""
        self.assertEqual(self.client.get('/accounts/password_reset/').status_code, 404)

        response = self.client.get('/accounts/password/reset/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/no_password_recovery.html')
        self.assertContains(response, 'No password recovery')
        # Above all, no form that takes an address and silently does nothing.
        self.assertNotContains(response, '<input type="email"')

    def test_allauths_signup_form_is_not_a_second_way_in(self):
        response = self.client.get('/accounts/signup/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('filemanager:register'))

    def test_changing_a_password_from_inside_the_account_works(self):
        self.client.login(username='grower', password='correct-horse-battery')

        response = self.client.get(reverse('password_change'))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse('password_change'), {
            'old_password': 'correct-horse-battery',
            'new_password1': 'a-different-long-one',
            'new_password2': 'a-different-long-one',
        })
        self.assertEqual(response.status_code, 302)

        self.client.logout()
        self.assertTrue(
            self.client.login(username='grower', password='a-different-long-one')
        )

    def test_the_profile_page_links_to_it(self):
        """With no reset, this link is the only route to a new password."""
        self.client.login(username='grower', password='correct-horse-battery')

        response = self.client.get('/profile/')

        self.assertContains(response, reverse('password_change'))

    def test_google_sign_in_is_untouched(self):
        response = self.client.get('/accounts/google/login/')
        self.assertIn(response.status_code, (302, 200))
