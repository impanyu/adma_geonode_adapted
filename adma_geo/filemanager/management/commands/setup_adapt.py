"""
Management command to set up the ADAPT root data folder.
Creates the top-level "ADAPT" folder that appears under Third Parties.
Run sync_adapt afterward to import the files.
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from filemanager.models import Folder, File

User = get_user_model()


class Command(BaseCommand):
    help = 'Set up the ADAPT root data folder for third-party data sync'

    def add_arguments(self, parser):
        parser.add_argument('--owner', type=str, default='admin')
        parser.add_argument('--force', action='store_true')
        parser.add_argument('--make-public', action='store_true')

    def handle(self, *args, **options):
        owner_username = options['owner']
        force = options['force']
        make_public = options['make_public']

        try:
            owner = User.objects.get(username=owner_username)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'User "{owner_username}" does not exist.'))
            return

        existing_folder = Folder.objects.filter(
            name='ADAPT', parent=None, is_third_party=True, third_party_source='adapt'
        ).first()

        if make_public and existing_folder:
            self._make_folder_public_recursive(existing_folder)
            return

        if existing_folder and not force:
            self.stdout.write(self.style.WARNING(f'ADAPT folder already exists (ID: {existing_folder.id})'))
            self.stdout.write('  Use --force to recreate or --make-public to publish.')
            return

        if existing_folder and force:
            self.stdout.write(self.style.WARNING('Deleting existing ADAPT folder...'))
            existing_folder.delete()

        adapt_folder = Folder.objects.create(
            name='ADAPT', owner=owner, parent=None, is_public=True,
            is_third_party=True, third_party_source='adapt', third_party_id='adapt_root',
        )

        self.stdout.write(self.style.SUCCESS('Successfully created ADAPT root folder!'))
        self.stdout.write(f'  Folder ID: {adapt_folder.id}')
        self.stdout.write('')
        self.stdout.write('Next step: python manage.py sync_adapt --once')

    def _make_folder_public_recursive(self, folder):
        folders_updated = 0
        files_updated = 0

        def update_recursive(current_folder):
            nonlocal folders_updated, files_updated
            if not current_folder.is_public:
                current_folder.is_public = True
                current_folder.save(update_fields=['is_public'])
                folders_updated += 1
            for file_obj in current_folder.files.filter(is_public=False):
                file_obj.is_public = True
                file_obj.save(update_fields=['is_public'])
                files_updated += 1
            for subfolder in current_folder.subfolders.all():
                update_recursive(subfolder)

        update_recursive(folder)
        self.stdout.write(self.style.SUCCESS(
            f'Done! Updated {folders_updated} folders and {files_updated} files to public.'
        ))
