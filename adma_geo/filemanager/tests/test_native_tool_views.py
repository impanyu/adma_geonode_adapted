"""
The pages and endpoints of the four native GIS tools.

These check the wiring rather than the maths -- that each page renders, that
the endpoints refuse what they should before a task is ever queued, and that
an unauthenticated caller gets JSON rather than a login redirect, which is the
failure that broke uploads for Sreeja.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from filemanager.models import File, Folder, Tool

User = get_user_model()

TOOL_PAGES = [
    'filemanager:zonal_statistics_tool',
    'filemanager:vegetation_index_tool',
    'filemanager:management_zones_tool',
    'filemanager:raster_clip_tool',
]

RUN_ENDPOINTS = [
    'filemanager:run_zonal_statistics',
    'filemanager:run_vegetation_index',
    'filemanager:run_management_zones',
    'filemanager:run_raster_clip_reproject',
]


class NativeToolPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')
        self.client.login(username='grower', password='correct-horse-battery')

    def test_every_page_renders(self):
        for name in TOOL_PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                # The shared picker has to be on the page or no input can be chosen.
                self.assertContains(response, 'ADMAFilePicker')
                self.assertNotContains(response, '{#')

    def test_pages_require_a_login(self):
        self.client.logout()
        for name in TOOL_PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn('/accounts/login/', response.url)

    def test_vegetation_index_lists_its_indices(self):
        response = self.client.get(reverse('filemanager:vegetation_index_tool'))
        for name in ('NDVI', 'NDRE', 'GNDVI', 'SAVI'):
            self.assertContains(response, name)

    def test_each_index_carries_the_bands_its_inputs_are_built_from(self):
        """
        The band rows are generated from data-bands. If an option loses it, or
        names a band the picker has no label for, the page renders an index
        that cannot be given any input -- and nothing server-side notices.
        """
        from filemanager.vegetation_index import INDEX_DEFINITIONS

        html = self.client.get(
            reverse('filemanager:vegetation_index_tool')
        ).content.decode()

        for definition in INDEX_DEFINITIONS.values():
            with self.subTest(index=definition.key):
                self.assertIn(
                    f'data-bands="{",".join(definition.bands)}"', html
                )
        # Every band named by any index needs a label in the page's BAND_LABELS.
        for definition in INDEX_DEFINITIONS.values():
            for band in definition.bands:
                with self.subTest(band=band):
                    self.assertIn(f'{band}:', html)


class NativeToolEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('grower', password='correct-horse-battery')
        self.other = User.objects.create_user('someone-else', password='correct-horse-battery')
        self.client.login(username='grower', password='correct-horse-battery')

        self.folder = Folder.objects.create(name='field', owner=self.user)
        self.shp = self._file('plots.shp', self.user)
        self.tif = self._file('imagery.tif', self.user)

    def _file(self, name, owner, is_public=False):
        f = File(name=name, folder=self.folder, owner=owner, is_public=is_public, file_size=1)
        f.file.name = f'uploads/{name}'
        f.save()
        return f

    def post(self, name, payload):
        return self.client.post(
            reverse(name), data=payload, content_type='application/json'
        )

    def test_unauthenticated_callers_get_json_not_a_redirect(self):
        """A redirect here is HTML, and response.json() on the page breaks."""
        self.client.logout()
        for name in RUN_ENDPOINTS:
            with self.subTest(endpoint=name):
                response = self.post(name, {})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response['Content-Type'], 'application/json')
                self.assertFalse(response.json()['success'])

    def test_a_raster_where_a_vector_belongs_is_refused(self):
        response = self.post('filemanager:run_zonal_statistics', {
            'vector_file_id': str(self.tif.id),
            'raster_file_id': str(self.tif.id),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('.shp', response.json()['error'])

    def test_another_users_private_file_is_refused(self):
        theirs = self._file('secret.shp', self.other)
        response = self.post('filemanager:run_zonal_statistics', {
            'vector_file_id': str(theirs.id),
            'raster_file_id': str(self.tif.id),
        })
        self.assertEqual(response.status_code, 403)

    def test_clip_and_reproject_needs_at_least_one_operation(self):
        response = self.post('filemanager:run_raster_clip_reproject', {
            'raster_file_id': str(self.tif.id),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('boundary', response.json()['error'])

    def test_a_non_numeric_epsg_is_refused(self):
        response = self.post('filemanager:run_raster_clip_reproject', {
            'raster_file_id': str(self.tif.id),
            'target_epsg': 'WGS84',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('EPSG', response.json()['error'])

    def test_management_zones_needs_a_column(self):
        response = self.post('filemanager:run_management_zones', {
            'file_id': str(self.shp.id),
            'columns': [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('column', response.json()['error'])

    def test_an_unknown_index_is_refused(self):
        response = self.post('filemanager:run_vegetation_index', {
            'index': 'not-an-index',
            'bands': {},
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('ndvi', response.json()['error'])

    def test_an_output_folder_belonging_to_someone_else_is_refused(self):
        theirs = Folder.objects.create(name='theirs', owner=self.other)
        response = self.post('filemanager:run_zonal_statistics', {
            'vector_file_id': str(self.shp.id),
            'raster_file_id': str(self.tif.id),
            'output_folder_id': str(theirs.id),
        })
        self.assertEqual(response.status_code, 400)

    @patch('filemanager.native_tool_tasks.run_zonal_statistics_task.delay')
    def test_a_valid_request_queues_the_task(self, delay):
        delay.return_value.id = 'task-1'

        response = self.post('filemanager:run_zonal_statistics', {
            'vector_file_id': str(self.shp.id),
            'raster_file_id': str(self.tif.id),
            'band': 2,
            'statistics': ['mean'],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['task_id'], 'task-1')
        # The caller's identity has to reach the task, or its own permission
        # check cannot run.
        self.assertEqual(delay.call_args.kwargs['requesting_user_id'], self.user.id)
        self.assertEqual(delay.call_args.kwargs['band'], 2)

    @patch('filemanager.native_tool_tasks.run_vegetation_index_task.delay')
    def test_one_file_can_supply_several_bands(self, delay):
        delay.return_value.id = 'task-2'

        response = self.post('filemanager:run_vegetation_index', {
            'index': 'ndvi',
            'bands': {
                'nir': {'file_id': str(self.tif.id), 'band': 2},
                'red': {'file_id': str(self.tif.id), 'band': 1},
            },
        })

        self.assertEqual(response.status_code, 200)
        band_files = delay.call_args.args[1]
        self.assertEqual(band_files['nir'], [str(self.tif.id), 2])
        self.assertEqual(band_files['red'], [str(self.tif.id), 1])


class NativeToolRegistrationTests(TestCase):
    def test_the_four_tools_are_registered_and_point_at_live_urls(self):
        Tool.create_system_tools()

        for slug in ['zonal-statistics', 'vegetation-index',
                     'management-zones', 'raster-clip-reproject']:
            with self.subTest(tool=slug):
                tool = Tool.objects.get(slug=slug)
                self.assertTrue(tool.is_system_tool)
                self.assertEqual(tool.status, 'available')
                # reverse() raises if the tool row names a URL that does not exist.
                self.assertTrue(reverse(tool.url_name))


class TaskModuleImportTests(TestCase):
    """
    filemanager.tasks is imported by every process that touches the app,
    including the web container, so importing it must not require the
    scientific stack. A module-level matplotlib import here once put the
    Celery worker into a startup crash loop when its image lagged behind
    requirements.txt -- taking down all background work, not just these tools.
    """

    def test_the_task_module_does_not_import_the_processing_stack(self):
        import ast
        import inspect

        from filemanager import native_tool_tasks

        tree = ast.parse(inspect.getsource(native_tool_tasks))
        heavy = {'zonal_statistics', 'vegetation_index',
                 'management_zones', 'raster_clip_reproject'}

        module_level = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }

        self.assertFalse(
            module_level & heavy,
            f'These belong inside the task bodies: {module_level & heavy}',
        )
