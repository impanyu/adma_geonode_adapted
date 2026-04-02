"""
Management command to backfill Realm5 weather station data since September 2022.

Pulls daily weather observations for all three weather stations under the realm5
public folder. Skips days that already have data.

Usage:
    python manage.py backfill_realm5                    # Full backfill from 2022-09-01
    python manage.py backfill_realm5 --start 2024-06-01 # Custom start date
    python manage.py backfill_realm5 --end 2025-12-31   # Custom end date
    python manage.py backfill_realm5 --station "Cow/Calf Weather Station"  # Single station
    python manage.py backfill_realm5 --dry-run           # Preview without downloading
"""

import json
import time
import logging
from datetime import date, timedelta, datetime

from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from django.conf import settings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill Realm5 weather station data since September 2022'

    def add_arguments(self, parser):
        parser.add_argument(
            '--start',
            type=str,
            default='2022-09-01',
            help='Start date in YYYY-MM-DD format (default: 2022-09-01)',
        )
        parser.add_argument(
            '--end',
            type=str,
            default=None,
            help='End date in YYYY-MM-DD format (default: yesterday)',
        )
        parser.add_argument(
            '--station',
            type=str,
            default=None,
            help='Only sync a specific station by name (e.g., "Cow/Calf Weather Station")',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be downloaded without actually downloading',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=50,
            help='Number of days to process before printing progress (default: 50)',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.5,
            help='Delay between API calls in seconds to avoid rate limiting (default: 0.5)',
        )

    def handle(self, *args, **options):
        from filemanager.models import Folder, File
        from filemanager.realm5_client import Realm5Client

        start_date = datetime.strptime(options['start'], '%Y-%m-%d').date()
        end_date = (
            datetime.strptime(options['end'], '%Y-%m-%d').date()
            if options['end']
            else date.today() - timedelta(days=1)
        )
        dry_run = options['dry_run']
        batch_size = options['batch_size']
        delay = options['delay']
        station_filter = options.get('station')

        self.stdout.write(self.style.NOTICE(
            f"Realm5 Backfill: {start_date} to {end_date}"
            f"{' (DRY RUN)' if dry_run else ''}"
        ))

        # Get API key
        api_key = getattr(settings, 'REALM5_API_KEY', None)
        if not api_key:
            self.stderr.write(self.style.ERROR(
                'REALM5_API_KEY not set. Add it to .env or settings.'
            ))
            return

        # Find the realm5 root folder
        realm5_folder = Folder.objects.filter(
            name='realm5',
            parent=None,
            is_third_party=True,
            third_party_source='realm5',
        ).first()

        if not realm5_folder:
            self.stderr.write(self.style.ERROR(
                'Realm5 root folder not found. Run "python manage.py setup_realm5" first.'
            ))
            return

        owner = realm5_folder.owner
        self.stdout.write(f"Realm5 folder owner: {owner.username}")

        # Get weather station subfolders
        station_folders = Folder.objects.filter(
            parent=realm5_folder,
            owner=owner,
        )

        if station_filter:
            station_folders = station_folders.filter(name=station_filter)

        stations = list(station_folders)
        if not stations:
            self.stderr.write(self.style.ERROR(
                f'No weather station folders found'
                f'{" matching: " + station_filter if station_filter else ""}.'
            ))
            return

        self.stdout.write(f"Found {len(stations)} station(s):")
        for s in stations:
            file_count = File.objects.filter(folder=s, owner=owner).count()
            self.stdout.write(f"  - {s.name} (third_party_id: {s.third_party_id}, {file_count} existing files)")

        # Initialize API client
        client = Realm5Client(api_key=api_key)

        # Test connection
        self.stdout.write("Testing Realm5 API connection...")
        try:
            devices = client.get_devices()
            self.stdout.write(self.style.SUCCESS(f"API OK — {len(devices)} devices found"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"API connection failed: {e}"))
            return

        # Build device lookup: dev_eui_hex -> dev_eui_numeric (API needs numeric)
        device_map = {}
        for device in devices:
            if device.get('device_type') != 'weather_station':
                continue
            dev_eui_hex = device.get('dev_eui_hex', '')
            dev_eui_numeric = str(device.get('dev_eui') or device.get('devEui') or device.get('id', ''))
            friendly_name = device.get('friendly_name') or device.get('name', '')
            device_map[dev_eui_hex] = {
                'numeric': dev_eui_numeric,
                'name': friendly_name,
            }
            device_map[friendly_name] = {
                'numeric': dev_eui_numeric,
                'hex': dev_eui_hex,
                'name': friendly_name,
            }

        # Calculate total days
        total_days = (end_date - start_date).days + 1
        self.stdout.write(f"Date range: {total_days} days per station")
        self.stdout.write("")

        # Process each station
        grand_total = {'skipped': 0, 'downloaded': 0, 'empty': 0, 'errors': 0}

        for station in stations:
            self.stdout.write(self.style.HTTP_INFO(
                f"=== {station.name} ==="
            ))

            # Resolve the device's numeric EUI for API calls
            dev_eui_hex = station.third_party_id or ''
            device_info = device_map.get(dev_eui_hex) or device_map.get(station.name)
            if not device_info:
                self.stderr.write(self.style.WARNING(
                    f"  Could not find device info for {station.name} "
                    f"(third_party_id: {dev_eui_hex}). Skipping."
                ))
                continue

            dev_eui_for_api = device_info['numeric']
            dev_eui = dev_eui_hex or device_info.get('hex', dev_eui_for_api)
            device_name = device_info['name'] or station.name

            self.stdout.write(f"  Device EUI: {dev_eui} (API: {dev_eui_for_api})")

            # Get all existing file names for this station in one query (fast lookup)
            existing_files = set(
                File.objects.filter(
                    folder=station,
                    owner=owner,
                ).values_list('name', flat=True)
            )
            self.stdout.write(f"  Existing files: {len(existing_files)}")

            # Iterate day by day
            current_date = start_date
            station_stats = {'skipped': 0, 'downloaded': 0, 'empty': 0, 'errors': 0}
            batch_start = time.time()

            while current_date <= end_date:
                file_name = f"{dev_eui}_{current_date.isoformat()}.json"

                # Check if already exists (in-memory lookup, very fast)
                if file_name in existing_files:
                    station_stats['skipped'] += 1
                    current_date += timedelta(days=1)
                    continue

                if dry_run:
                    self.stdout.write(f"  [DRY RUN] Would download: {file_name}")
                    station_stats['downloaded'] += 1
                    current_date += timedelta(days=1)
                    continue

                # Fetch from API
                try:
                    observations = client.get_observations_by_day(
                        dev_eui_for_api, current_date
                    )

                    if observations:
                        # Build observation list
                        observations_list = [
                            {'timestamp': ts, **obs_data}
                            for ts, obs_data in observations.items()
                        ]
                        observations_list.sort(key=lambda x: x['timestamp'])

                        # Create JSON
                        observation_data = {
                            'dev_eui': dev_eui,
                            'dev_eui_numeric': dev_eui_for_api,
                            'device_name': device_name,
                            'device_type': 'weather_station',
                            'date': current_date.isoformat(),
                            'observation_count': len(observations_list),
                            'observations': observations_list,
                            'fetched_at': date.today().isoformat(),
                        }
                        json_content = json.dumps(observation_data, indent=2, default=str)

                        # Save to Django
                        file_obj = File(
                            name=file_name,
                            folder=station,
                            owner=owner,
                            file_type='text',
                            mime_type='application/json',
                            is_public=True,
                            is_third_party=True,
                            third_party_source='realm5',
                            third_party_id=f"{dev_eui}_{current_date.isoformat()}",
                        )
                        file_obj.file.save(file_name, ContentFile(json_content.encode('utf-8')))
                        file_obj.file_size = len(json_content)
                        file_obj.save()

                        station_stats['downloaded'] += 1
                        existing_files.add(file_name)
                    else:
                        station_stats['empty'] += 1

                except Exception as e:
                    station_stats['errors'] += 1
                    if station_stats['errors'] <= 5:
                        self.stderr.write(self.style.WARNING(
                            f"  Error on {current_date}: {e}"
                        ))
                    elif station_stats['errors'] == 6:
                        self.stderr.write(self.style.WARNING(
                            "  (suppressing further error messages)"
                        ))

                # Progress report every batch_size days
                days_processed = station_stats['skipped'] + station_stats['downloaded'] + station_stats['empty'] + station_stats['errors']
                if days_processed > 0 and days_processed % batch_size == 0:
                    elapsed = time.time() - batch_start
                    rate = batch_size / elapsed if elapsed > 0 else 0
                    remaining = total_days - days_processed
                    eta = remaining / rate if rate > 0 else 0
                    self.stdout.write(
                        f"  Progress: {days_processed}/{total_days} days "
                        f"({station_stats['downloaded']} new, {station_stats['skipped']} skipped, "
                        f"{station_stats['empty']} empty, {station_stats['errors']} errors) "
                        f"[{rate:.1f} days/s, ETA: {eta/60:.0f}m]"
                    )
                    batch_start = time.time()

                # Rate limiting
                if not dry_run and station_stats['downloaded'] > 0:
                    time.sleep(delay)

                current_date += timedelta(days=1)

            # Station summary
            self.stdout.write(self.style.SUCCESS(
                f"  Done: {station_stats['downloaded']} downloaded, "
                f"{station_stats['skipped']} skipped, "
                f"{station_stats['empty']} empty days, "
                f"{station_stats['errors']} errors"
            ))
            self.stdout.write("")

            for k in grand_total:
                grand_total[k] += station_stats[k]

        # Grand summary
        self.stdout.write(self.style.SUCCESS(
            f"=== TOTAL ==="
        ))
        self.stdout.write(
            f"Downloaded: {grand_total['downloaded']} new files"
        )
        self.stdout.write(
            f"Skipped:    {grand_total['skipped']} (already exist)"
        )
        self.stdout.write(
            f"Empty:      {grand_total['empty']} (no data from API)"
        )
        self.stdout.write(
            f"Errors:     {grand_total['errors']}"
        )
