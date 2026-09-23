import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from filemanager.models import File


class GeoJSONMapTests(TestCase):
    def setUp(self):
        media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)

        self.owner = get_user_model().objects.create_user(username='geo_owner', password='x')
        self.other = get_user_model().objects.create_user(username='geo_other', password='x')
        self.file = File(name='sample.geojson', owner=self.owner, gis_status='error')
        self.file.file.save(
            'sample.geojson',
            ContentFile(b'{"type":"FeatureCollection","features":[]}'),
        )
        self.file.refresh_from_db()

    def test_owner_can_view_geojson_even_after_failed_geoserver_publish(self):
        self.client.force_login(self.owner)
        map_response = self.client.get(reverse('filemanager:map_viewer', args=[self.file.id]))
        self.assertEqual(map_response.status_code, 200)
        self.assertContains(map_response, 'geojson-status')
        self.assertContains(map_response, reverse('filemanager:geojson_data', args=[self.file.id]))

        data_response = self.client.get(reverse('filemanager:geojson_data', args=[self.file.id]))
        self.assertEqual(data_response.status_code, 200)
        self.assertEqual(data_response['Content-Type'], 'application/geo+json')
        self.assertIn(b'FeatureCollection', b''.join(data_response.streaming_content))

    def test_private_geojson_is_not_exposed_to_other_users(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('filemanager:geojson_data', args=[self.file.id]))
        self.assertEqual(response.status_code, 404)

    def test_public_geojson_is_available_from_public_map(self):
        self.file.is_public = True
        self.file.save(update_fields=['is_public'])
        map_response = self.client.get(reverse('filemanager:public_map_viewer', args=[self.file.id]))
        self.assertEqual(map_response.status_code, 200)
        self.assertContains(map_response, 'geojson-status')
        data_response = self.client.get(reverse('filemanager:geojson_data', args=[self.file.id]))
        self.assertEqual(data_response.status_code, 200)
