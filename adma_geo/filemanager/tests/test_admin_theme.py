from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class AdminThemeTests(TestCase):
    """The ADMA header and dashboard replace Django's header, sidebar and index."""

    def setUp(self):
        self.superuser = User.objects.create_superuser('root', 'r@x.com', 'pw')

    def test_header_has_the_navbar_and_user_menu_not_djangos_user_tools(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:auth_user_changelist'))
        self.assertContains(response, 'class="adma-topbar"')
        self.assertContains(response, 'class="adma-menu"')
        self.assertContains(response, 'id="logout-form"')          # logging out still posts
        self.assertNotContains(response, 'id="user-tools"')
        self.assertNotContains(response, 'id="nav-sidebar"')

    def test_index_is_the_dashboard_with_apps_and_recent_actions(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:index'))
        self.assertContains(response, 'Welcome back, root')
        self.assertContains(response, 'class="dash-card app-auth"')
        self.assertContains(response, reverse('admin:auth_user_changelist'))
        self.assertContains(response, 'id="recent-actions-module"')

    def test_login_page_shows_the_brand_without_the_user_menu(self):
        response = self.client.get(reverse('admin:login'))
        self.assertContains(response, 'class="adma-topbar"')
        self.assertNotContains(response, 'class="adma-menu"')
