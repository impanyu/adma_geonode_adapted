"""
The plumbing every tool task repeats: decide where output goes, then register
what was written as File rows.

Each of the existing tool tasks carries its own copy of these sixty-odd lines,
which is how they came to disagree in small ways -- one skips files that the
processing step reported but never wrote, another crashes on them. New tools
call in here instead.

A processing module returns ``output_files``: a mapping of a role name to a
path, or to a list of paths when one logical output is several files on disk.
A shapefile is the usual reason for the list form -- .shp is useless without
its .dbf, .shx and .prj -- so those arrive as ``{'zones': [...4 paths...]}``
and every component is registered.
"""
import logging
import os

from django.conf import settings

from .models import File, Folder

logger = logging.getLogger(__name__)


def resolve_output_folder(source_file, output_folder_id=None, default_name='tool_output'):
    """
    Return ``(folder, absolute_directory)`` for a tool's output.

    With no folder chosen, output lands in a subfolder of the input's own
    folder, reused if a previous run already made it. The directory is created
    on disk, so a caller can write into it immediately.
    """
    if output_folder_id:
        try:
            folder = Folder.objects.get(id=output_folder_id)
        except Folder.DoesNotExist:
            raise ValueError(f'Output folder {output_folder_id} not found')
    else:
        folder = Folder.objects.filter(
            name=default_name,
            parent=source_file.folder,
            owner=source_file.owner,
        ).first()
        if folder is None:
            folder = Folder.objects.create(
                name=default_name,
                parent=source_file.folder,
                owner=source_file.owner,
                is_public=source_file.is_public,
            )
            logger.info('Created output folder %s (%s)', default_name, folder.id)

    directory = os.path.join(settings.MEDIA_ROOT, 'uploads', folder.get_full_path())
    os.makedirs(directory, exist_ok=True)
    return folder, directory


def _iter_paths(output_files):
    """Flatten the role -> path / role -> [paths] mapping into plain paths."""
    for role, value in (output_files or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            for path in value:
                if path:
                    yield role, path
        else:
            yield role, value


def register_outputs(output_files, folder, owner, is_public=False):
    """
    Create (or refresh) a File row per written output.

    Returns the list of registered files. A path the processing step named but
    did not actually write is skipped with a warning rather than aborting the
    run: partial output is still worth handing back, and the caller reports
    what it got.
    """
    registered = []

    for role, path in _iter_paths(output_files):
        if not os.path.exists(path):
            logger.warning('Tool named output %r at %s but nothing was written there', role, path)
            continue

        media_root = str(settings.MEDIA_ROOT)
        relative_path = os.path.relpath(path, media_root) if path.startswith(media_root) else path
        name = os.path.basename(path)
        size = os.path.getsize(path)

        existing = File.objects.filter(name=name, folder=folder, owner=owner).first()
        if existing:
            existing.file_size = size
            existing.file.name = relative_path
            existing.save(update_fields=['file_size', 'file', 'updated_at'])
            registered.append({'role': role, 'name': name, 'id': str(existing.id), 'updated': True})
        else:
            new_file = File(
                name=name,
                folder=folder,
                owner=owner,
                file_size=size,
                is_public=is_public,
            )
            new_file.file.name = relative_path
            new_file.save()
            registered.append({'role': role, 'name': name, 'id': str(new_file.id), 'updated': False})

    return registered
