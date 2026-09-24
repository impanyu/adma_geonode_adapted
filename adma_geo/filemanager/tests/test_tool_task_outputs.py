"""
Characterisation tests for the four original tool tasks' output handling.

Each carries its own copy of "work out the output folder, then register what
was written" -- the block shape_to_json used before it moved onto tool_io.
These pin the behaviour of that part so the rest can follow without guessing.

The processing step of each tool is stubbed. These tools take shapefiles with
particular agronomy columns, and building valid inputs for all four would test
the agronomy rather than the file handling, which is what is being changed.
"""
import os
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from filemanager import tasks
from filemanager.models import File, Folder

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='adma-tt-')


def write(directory, name, text='x'):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'w') as handle:
        handle.write(text)
    return path


@override_settings(MEDIA_ROOT=MEDIA)
class ToolOutputTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('grower', password='x')
        self.folder = Folder.objects.create(name='field-9', owner=self.user)
        self.directory = os.path.join(MEDIA, 'uploads', self.folder.get_full_path())
        os.makedirs(self.directory, exist_ok=True)

    def make_file(self, name):
        path = write(self.directory, name)
        file_obj = File(name=name, folder=self.folder, owner=self.user,
                        file_size=os.path.getsize(path))
        file_obj.file.name = os.path.relpath(path, MEDIA)
        file_obj.save()
        return file_obj

    @staticmethod
    def fake_seeding(input_path, output_folder=None, plot_path=None, **kwargs):
        """Matches process_seeding_data: writes files, returns a results dict."""
        base = os.path.join(output_folder, 'polys')
        for ext in ('.shp', '.shx', '.dbf'):
            write(output_folder, 'polys' + ext)
        return {
            'polygons_path': base + '.shp',
            'summary_path': write(output_folder, 'summary.csv'),
            'plot_path': write(output_folder, 'preview.png') if plot_path else None,
            'products_processed': 1,
            'product_col_used': 'Product',
            'width_col_used': 'Width',
            'rate_col_used': 'Rate',
        }

    # --- seeding tool ------------------------------------------------------

    def test_seeding_tool_registers_output_in_its_own_folder(self):
        source = self.make_file('points.shp')

        with patch('filemanager.SeedingPolygonTool_SV.process_seeding_data',
                   side_effect=self.fake_seeding):
            result = tasks.run_seeding_tool_task.apply(args=[str(source.id)]).get()

        self.assertTrue(result['success'], result.get('error'))
        folder = Folder.objects.get(name='seeding_tool_output')
        self.assertEqual(folder.parent, self.folder)
        names = set(File.objects.filter(folder=folder).values_list('name', flat=True))
        # The sidecars matter: a .shp alone cannot be opened.
        self.assertTrue({'polys.shp', 'polys.dbf', 'polys.shx'} <= names, names)
        self.assertIn('summary.csv', names)

    def test_seeding_tool_reuses_its_folder_on_a_second_run(self):
        source = self.make_file('points.shp')

        with patch('filemanager.SeedingPolygonTool_SV.process_seeding_data',
                   side_effect=self.fake_seeding):
            tasks.run_seeding_tool_task.apply(args=[str(source.id)]).get()
            tasks.run_seeding_tool_task.apply(args=[str(source.id)]).get()

        self.assertEqual(Folder.objects.filter(name='seeding_tool_output').count(), 1)
        self.assertEqual(File.objects.filter(name='polys.shp').count(), 1)
        self.assertEqual(File.objects.filter(name='summary.csv').count(), 1)

    def test_seeding_tool_honours_a_chosen_folder(self):
        source = self.make_file('points.shp')
        chosen = Folder.objects.create(name='chosen', owner=self.user)

        with patch('filemanager.SeedingPolygonTool_SV.process_seeding_data',
                   side_effect=self.fake_seeding):
            tasks.run_seeding_tool_task.apply(
                args=[str(source.id)], kwargs={'output_dir_id': str(chosen.id)}
            ).get()

        self.assertTrue(File.objects.filter(name='polys.shp', folder=chosen).exists())
        self.assertFalse(Folder.objects.filter(name='seeding_tool_output').exists())

    def test_seeding_tool_refuses_another_users_file(self):
        source = self.make_file('points.shp')
        intruder = User.objects.create_user('intruder', password='x')

        result = tasks.run_seeding_tool_task.apply(
            args=[str(source.id)], kwargs={'requesting_user_id': intruder.id}
        ).get()

        self.assertFalse(result['success'])

    # --- yield summary -----------------------------------------------------

    def test_yield_summary_registers_output(self):
        treatment = self.make_file('treatment.shp')
        yields = self.make_file('yield.shp')

        def fake(*args, **kwargs):
            out = kwargs.get('output_dir') or kwargs.get('output_folder') or args[2]
            return True, 'done', {'summary_xlsx': write(out, 'summary.xlsx')}

        with patch('filemanager.yield_summary_tool_single_V4.process_yield_summary',
                   side_effect=fake):
            result = tasks.run_yield_summary_tool_task.apply(
                args=[str(treatment.id), str(yields.id), [100, 150]]
            ).get()

        self.assertTrue(result['success'], result.get('error'))
        folder = Folder.objects.get(name='yield_summary_output')
        self.assertTrue(File.objects.filter(name='summary.xlsx', folder=folder).exists())

    # --- valid yield extractor --------------------------------------------

    def test_valid_yield_extractor_registers_output(self):
        plots = self.make_file('plots.shp')
        applied = self.make_file('applied.shp')
        harvest = self.make_file('harvest.shp')

        def fake(args):
            write(args.out, 'clean.shp')

        with patch('filemanager.ValidYieldExtractorTool.run', side_effect=fake):
            result = tasks.run_valid_yield_extractor_task.apply(
                args=[str(plots.id), str(applied.id), str(harvest.id)]
            ).get()

        self.assertTrue(result['success'], result.get('error'))
        folder = Folder.objects.get(name='yield_cleaning_output')
        self.assertTrue(File.objects.filter(name='clean.shp', folder=folder).exists())
