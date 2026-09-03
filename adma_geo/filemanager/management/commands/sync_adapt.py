"""
Management command to sync the ADAPT data warehouse into ADMA by REFERENCE.

Walks a subdirectory of the mounted ADAPT share (default "Data Management"),
recreates the folder tree under the ADAPT third-party root, and creates File
records that REFERENCE the files on the mount (stored in third_party_url)
instead of copying their content. ADMA streams the bytes live from the mount
when a user views/downloads them, so nothing is duplicated and the view always
reflects the warehouse.

Idempotent and read-only toward the source.

Usage:
    python manage.py sync_adapt --once
    python manage.py sync_adapt --once --subdir "Data Management"
"""
import os
import mimetypes
from pathlib import Path

from django.core.management.base import BaseCommand
from filemanager.models import Folder, File


def guess_file_type(filename):
    ext = Path(filename).suffix.lower()
    if ext == '.csv':
        return 'csv'
    if ext in ('.xlsx', '.xls'):
        return 'spreadsheet'
    if ext in ('.shp', '.geojson', '.kml', '.tif', '.tiff', '.gpkg', '.json'):
        return 'gis'
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
        return 'image'
    if ext in ('.txt', '.md', '.log'):
        return 'text'
    if ext in ('.pdf', '.doc', '.docx'):
        return 'document'
    return 'other'


class Command(BaseCommand):
    help = 'Sync the ADAPT warehouse into ADMA by reference (Third Parties)'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--mount', type=str, default='/adapt')
        parser.add_argument('--subdir', type=str, default='Data Management')

    def handle(self, *args, **options):
        mount = options['mount']
        subdir = options['subdir']
        run = options['once']

        source_root = os.path.join(mount, subdir)

        if not os.path.isdir(source_root):
            self.stderr.write(self.style.ERROR(f'Source directory not found/readable: {source_root}'))
            self.stderr.write('Check: docker compose exec django ls "/adapt/Data Management"')
            return

        adapt_root = Folder.objects.filter(
            name='ADAPT', parent=None, is_third_party=True, third_party_source='adapt'
        ).first()
        if not adapt_root:
            self.stderr.write(self.style.ERROR("ADAPT root not found. Run 'python manage.py setup_adapt' first."))
            return

        owner = adapt_root.owner

        if not run:
            self.stdout.write(self.style.WARNING('Dry run: pass --once to actually import.'))
            self.stdout.write(f'  Would reference from: {source_root}')
            return

        self.stdout.write(f'Referencing from: {source_root}')
        self.stdout.write(f'Into: ADAPT (owner: {owner.username})')
        self.stdout.write('')

        stats = {'folders_created': 0, 'files_created': 0, 'skipped': 0, 'errors': 0}
        dir_map = {os.path.abspath(source_root): adapt_root}

        for current_dir, subdirs, filenames in os.walk(source_root):
            subdirs.sort()
            filenames.sort()
            abs_current = os.path.abspath(current_dir)
            parent_folder = dir_map.get(abs_current)
            if parent_folder is None:
                continue

            for dname in subdirs:
                abs_child = os.path.join(abs_current, dname)
                try:
                    child_folder, created = Folder.objects.get_or_create(
                        name=dname, parent=parent_folder, owner=owner,
                        defaults={
                            'is_public': True, 'is_third_party': True,
                            'third_party_source': 'adapt',
                            'third_party_id': os.path.relpath(abs_child, os.path.abspath(source_root)),
                        },
                    )
                    dir_map[abs_child] = child_folder
                    if created:
                        stats['folders_created'] += 1
                        self.stdout.write(f'  + folder: {child_folder.get_full_path()}')
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(self.style.ERROR(f'  ! folder error ({dname}): {e}'))

            for fname in filenames:
                abs_file = os.path.join(abs_current, fname)
                if fname.startswith('.'):
                    continue
                if File.objects.filter(name=fname, folder=parent_folder, owner=owner).exists():
                    stats['skipped'] += 1
                    continue
                try:
                    size = os.path.getsize(abs_file)
                    mime, _ = mimetypes.guess_type(fname)
                    rel_id = os.path.relpath(abs_file, os.path.abspath(source_root))

                    # Reference the file on the mount; do NOT copy content.
                    file_obj = File(
                        name=fname,
                        folder=parent_folder,
                        owner=owner,
                        file_size=size,
                        file_type=guess_file_type(fname),
                        mime_type=mime or '',
                        is_public=True,
                        is_third_party=True,
                        third_party_source='adapt',
                        third_party_id=rel_id,
                        third_party_url=abs_file,   # absolute path on the mount inside the container
                    )
                    file_obj.save()
                    stats['files_created'] += 1
                    self.stdout.write(f'  + file (ref): {rel_id} ({size} bytes)')
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(self.style.ERROR(f'  ! file error ({fname}): {e}'))

        self.stdout.write('')
        if stats['errors'] == 0:
            self.stdout.write(self.style.SUCCESS('Sync (by reference) completed.'))
        else:
            self.stdout.write(self.style.WARNING('Sync completed with some errors.'))
        self.stdout.write(f"  Folders created: {stats['folders_created']}")
        self.stdout.write(f"  Files created:   {stats['files_created']}")
        self.stdout.write(f"  Skipped:         {stats['skipped']}")
        self.stdout.write(f"  Errors:          {stats['errors']}")
