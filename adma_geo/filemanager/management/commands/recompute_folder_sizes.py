"""
Recompute and cache each folder's total size and total file count.

Works entirely from the database (no mount access), computing bottom-up so
each folder's cached totals include all descendants. Populates the
cached_total_size and cached_total_file_count fields used by third-party
(reference) folders for fast page loads.

Usage:
    python manage.py recompute_folder_sizes
    python manage.py recompute_folder_sizes --source adapt   # only third-party 'adapt' tree
"""
from django.core.management.base import BaseCommand
from django.db.models import Sum, Count
from filemanager.models import Folder, File


class Command(BaseCommand):
    help = 'Recompute cached total size and file count for folders (bottom-up).'

    def add_arguments(self, parser):
        parser.add_argument('--source', type=str, default=None,
                            help="Limit to a third_party_source (e.g. 'adapt'). Default: all folders.")

    def handle(self, *args, **options):
        source = options['source']

        qs = Folder.objects.all()
        if source:
            qs = qs.filter(third_party_source=source)

        folders = list(qs)
        self.stdout.write(f'Computing cached totals for {len(folders)} folders...')

        # Direct (non-recursive) size and count per folder, via one aggregate query each.
        # Build a map: folder_id -> (direct_size, direct_count)
        direct = {}
        for f in folders:
            agg = File.objects.filter(folder=f).aggregate(
                s=Sum('file_size'), c=Count('id')
            )
            direct[f.id] = (agg['s'] or 0, agg['c'] or 0)

        # Build children map
        children = {}
        for f in folders:
            children.setdefault(f.parent_id, []).append(f)

        # Recursive rollup with memoization
        size_cache = {}
        count_cache = {}

        def rollup(folder):
            if folder.id in size_cache:
                return size_cache[folder.id], count_cache[folder.id]
            d_size, d_count = direct.get(folder.id, (0, 0))
            total_size = d_size
            total_count = d_count
            for child in children.get(folder.id, []):
                cs, cc = rollup(child)
                total_size += cs
                total_count += cc
            size_cache[folder.id] = total_size
            count_cache[folder.id] = total_count
            return total_size, total_count

        updated = 0
        for f in folders:
            ts, tc = rollup(f)
            if f.cached_total_size != ts or f.cached_total_file_count != tc:
                f.cached_total_size = ts
                f.cached_total_file_count = tc
                f.save(update_fields=['cached_total_size', 'cached_total_file_count'])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done. Updated {updated} of {len(folders)} folders.'
        ))
