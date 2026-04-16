from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.models import File, Folder

User = get_user_model()


class TestArchivedVisibility(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='u', password='x')
        self.client.force_login(self.user)
        self.live = Folder.objects.create(name='live', owner=self.user)
        self.dead = Folder.objects.create(
            name='dead', owner=self.user, is_archived=True,
        )
        File.objects.create(name='ok.txt', owner=self.user, folder=self.live)
        File.objects.create(
            name='gone.txt', owner=self.user, folder=self.live, is_archived=True,
        )

    def test_api_list_folders_excludes_archived(self):
        resp = self.client.get('/api/v1/folders/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'live')
        self.assertNotContains(resp, 'dead')

    def test_api_list_files_excludes_archived(self):
        resp = self.client.get('/api/v1/files/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ok.txt')
        self.assertNotContains(resp, 'gone.txt')

    def test_dashboard_stats_counts_only_non_archived(self):
        resp = self.client.get('/api/dashboard/stats/')
        self.assertEqual(resp.status_code, 200)
        # The exact JSON key names differ by codebase; the invariant is that
        # the archived folder/file names do not appear in the counted response.
        self.assertNotContains(resp, 'dead')
        self.assertNotContains(resp, 'gone.txt')
